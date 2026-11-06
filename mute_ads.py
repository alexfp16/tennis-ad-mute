"""Mutes the ads in a tennis stream you are watching in Chrome.

Every 2 seconds it grabs the Chrome tab, keeps just the video, and asks d1 one question: is this part of a live tennis broadcast?
It also learns the broadcaster's corner logo by itself in the first few minutes of play.

    mute fast   : one low frame with no logo on screen mutes at once
    unmute slow : five match frames in a row bring the sound back
    logo on     : never mutes, so close-ups of players and umpires are safe

It sets the video's muted flag and nothing else. Closing it unmutes. Start Chrome with
--remote-debugging-port=9224. DRM video captures as black, so this won't work there; leave that alone.
Frames are thrown away unless you pass --save. Saved frames are copies of a broadcast: keep them to yourself.

    python mute_ads.py                  use the playing video it finds
    python mute_ads.py --tab tennis     choose by words in the tab's title or address
    python mute_ads.py --no-logo        d1 alone (mutes after 2 low frames instead of 1)
    python mute_ads.py --save           keep every frame in captures\\, for replay and testing later
    python mute_ads.py --replay DIR     run over saved frames with no Chrome and no muting
"""
import argparse, base64, json, os, pathlib, signal, subprocess, sys, time, urllib.request

import cv2
import numpy as np
import websocket

HERE = pathlib.Path(__file__).parent
SERVER = os.environ.get("LLAMA_SERVER", "llama-server")
MODEL = os.environ.get("D1_MODEL", str(HERE / "d1-2B-Q8_0.gguf"))
MMPROJ = os.environ.get("D1_MMPROJ", str(HERE / "mmproj-d1-2B-F16.gguf"))
CHROME, D1 = "http://127.0.0.1:9224", "http://127.0.0.1:10098"

EVERY_S = 2
LOW = 0.5
MUTE_AFTER_WITH_LOGO, MUTE_AFTER_NO_LOGO, UNMUTE_AFTER = 1, 2, 5
LEARN_FRAMES, LEARN_SCORE, LOGO_SCORE, LOGO_STILL, RELEARN_AFTER = 40, 0.8, 0.6, 22, 60
GIVE_UP_FAILS = 15
MIN_VIDEO_WIDTH, MIN_VIDEO_HEIGHT = 160, 90
STATE = "One frame from a live sports television broadcast."
QUESTIONS = {"is_match": {"type": "noul", "instructions": "Is this part of a live tennis broadcast?"}}
FIND_VIDEO = """(() => { let best = 0; for (const v of document.querySelectorAll('video'))
  if (v.videoWidth && !v.paused) best = Math.max(best, v.videoWidth * v.videoHeight); return best })()"""
VIDEO_BOX = """(() => { let best = null, size = 0; for (const v of document.querySelectorAll('video'))
  if (v.videoWidth && !v.paused && v.videoWidth * v.videoHeight > size) { best = v; size = v.videoWidth * v.videoHeight }
  if (!best) return null; const r = best.getBoundingClientRect();
  const fit = Math.min(r.width / best.videoWidth, r.height / best.videoHeight);
  const w = best.videoWidth * fit, h = best.videoHeight * fit;
  return [r.left + (r.width - w) / 2, r.top + (r.height - h) / 2, w, h] })()"""


def say(text):
    print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)


# ---- Chrome ----------------------------------------------------------------------------------

def targets():
    return json.load(urllib.request.urlopen(CHROME + "/json/list", timeout=5))


def cdp(target, method, params=None, timeout=10):
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                return m.get("result", {})
    finally:
        ws.close()


def evaluate(target, expr):
    return cdp(target, "Runtime.evaluate", {"expression": expr, "returnByValue": True}, 4).get("result", {}).get("value")


def top_page(t, by_id):
    while t.get("parentId") in by_id:
        t = by_id[t["parentId"]]
    return t


def find_video(words):
    """Returns (the frame holding the video, the tab to capture), biggest playing video first."""
    all_targets = [t for t in targets() if t["type"] in ("page", "iframe")]
    by_id = {t["id"]: t for t in all_targets}
    found = []
    for t in all_targets:
        if t["url"].startswith(("chrome", "devtools")):
            continue
        page = top_page(t, by_id)
        if words and words.lower() not in f"{page['title']} {page['url']}".lower():
            continue
        try:
            size = evaluate(t, FIND_VIDEO)
        except Exception:
            continue
        if size:
            found.append((size, t, page))
    if not found:
        return None
    found.sort(key=lambda x: -x[0])
    if len(found) > 1:
        say("more than one video is playing, using the biggest. Choose another with --tab:")
        for _, _, p in found:
            say(f"    {p['title'][:70]}")
    return found[0][1], found[0][2]


def crop_to_video(frame, video, page):
    """Keeps only the video's picture, so a small player in a big window still fills d1's view."""
    try:
        x, y, w, h = evaluate(video, VIDEO_BOX)
        by_id = {t["id"]: t for t in targets()}
        t = video
        while t["id"] != page["id"]:  # add the offset of each iframe it sits in
            parent = by_id[t["parentId"]]
            owner = cdp(parent, "DOM.getFrameOwner", {"frameId": t["id"]})
            quad = cdp(parent, "DOM.getContentQuads", {"backendNodeId": owner["backendNodeId"]})["quads"][0]
            x, y, t = x + quad[0], y + quad[1], parent
        scale = frame.shape[1] / evaluate(page, "innerWidth")
    except (OSError, KeyError, IndexError, TypeError, ZeroDivisionError, websocket.WebSocketException):
        return frame
    left, top = max(0, round(x * scale)), max(0, round(y * scale))
    right, bottom = min(frame.shape[1], round((x + w) * scale)), min(frame.shape[0], round((y + h) * scale))
    if right - left < MIN_VIDEO_WIDTH or bottom - top < MIN_VIDEO_HEIGHT:
        return frame
    return frame[top:bottom, left:right]


def set_muted(target, on):
    return evaluate(target, f"document.querySelectorAll('video').forEach(v => v.muted = {'true' if on else 'false'})")


# ---- d1 --------------------------------------------------------------------------------------

def d1_up():
    try:
        return b"ok" in urllib.request.urlopen(D1 + "/health", timeout=2).read()
    except Exception:
        return False


def start_d1():
    """Returns the process if this script started the server, None if one was already running."""
    if d1_up():
        return None
    say("loading d1...")
    log = open(HERE / "d1_server.log", "w")
    p = subprocess.Popen([SERVER, "-m", MODEL, "--mmproj", MMPROJ, "-ngl", "99", "-fa", "on", "-np", "1", "-c", "8192",
                          "-b", "8192", "-ub", "8192", "--port", "10098", "--host", "127.0.0.1"], stdout=log, stderr=log)
    for _ in range(120):
        if d1_up():
            return p
        if p.poll() is not None:
            break
        time.sleep(.5)
    sys.exit(f"d1 didn't start, see {HERE / 'd1_server.log'}")


def ask_d1(frame):
    small = cv2.resize(frame, (640, round(frame.shape[0] * 640 / frame.shape[1])), interpolation=cv2.INTER_AREA)
    img = "data:image/jpeg;base64," + base64.b64encode(cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()
    body = json.dumps({"state": STATE, "images": [img], "questions": QUESTIONS}).encode()
    req = urllib.request.Request(D1 + "/v1/systemone", body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))["answers"]["is_match"]["noul"]


# ---- corner logo, learned from the first few minutes of play -----------------------------------

class Logo:
    W = 1280
    CORNERS = {"top right": (960, 0, 320, 180), "top left": (0, 0, 320, 180)}

    def __init__(self):
        self.shape = None
        self.reset()

    def reset(self):
        self.stacks = {k: [] for k in self.CORNERS}
        self.seen, self.absent = 0, 0
        self.template = self.box = None
        self.done = False

    def gray(self, frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (self.W, round(g.shape[0] * self.W / g.shape[1])), interpolation=cv2.INTER_AREA)
        if self.shape not in (None, g.shape):  # window resized: the logo moved, start again
            say("the picture changed shape, relearning the logo")
            self.reset()
        self.shape = g.shape
        return g

    def learn(self, gray):
        self.seen += 1
        if self.seen % 2:  # every other match frame, so one camera angle doesn't fill the stack
            return
        for k, (x, y, w, h) in self.CORNERS.items():
            self.stacks[k].append(gray[y:y + h, x:x + w])
        if len(self.stacks["top right"]) < LEARN_FRAMES:
            return
        best = None
        for k, stack in self.stacks.items():
            s = np.stack(stack).astype("float32")
            med, std = np.median(s, axis=0), s.std(axis=0)
            # A logo holds still while the picture moves behind it, and stands out from what's around it.
            mask = ((std < LOGO_STILL) & (np.abs(med - cv2.blur(med, (31, 31))) > 25)).astype("uint8")
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), "uint8"))
            n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if 24 <= w <= 220 and 24 <= h <= 130 and area >= 250 and (best is None or area > best[0]):
                    best = (area, k, x, y, w, h, med)
        if best is None:
            say("no steady corner logo yet, carrying on with d1 alone and trying again")
            self.reset()
            return
        self.done, self.stacks = True, None
        _, k, x, y, w, h, med = best
        self.template = med[y:y + h, x:x + w].astype("uint8")
        cx, cy = self.CORNERS[k][:2]
        self.box = (cx + x, cy + y, w, h)
        say(f"learned the {k} logo, {w}x{h}")

    def on_screen(self, gray):
        if self.template is None:
            return None
        x, y, w, h = self.box
        pad = 12
        roi = gray[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        return float(cv2.minMaxLoc(cv2.matchTemplate(roi, self.template, cv2.TM_CCOEFF_NORMED))[1])

    def check_still_valid(self, score, seen):
        """A long run of match frames without the logo means it changed, so learn the new one."""
        if self.template is None:
            return
        self.absent = self.absent + 1 if score > LEARN_SCORE and seen <= LOGO_SCORE else 0
        if self.absent >= RELEARN_AFTER:
            say("the logo seems to have changed, relearning it")
            self.reset()


# ---- the rule --------------------------------------------------------------------------------

class Rule:
    def __init__(self):
        self.muted, self.lows, self.highs = False, 0, 0

    def step(self, score, logo):
        low = score < LOW and not (logo is not None and logo > LOGO_SCORE)
        self.lows, self.highs = (self.lows + 1, 0) if low else (0, self.highs + 1)
        if self.lows >= (MUTE_AFTER_NO_LOGO if logo is None else MUTE_AFTER_WITH_LOGO):
            self.muted = True
        elif self.highs >= UNMUTE_AFTER:
            self.muted = False
        return self.muted


def judge(frame, logo, rule, use_logo):
    score = ask_d1(frame)
    gray = logo.gray(frame) if use_logo else None
    if use_logo and not logo.done and score > LEARN_SCORE:
        logo.learn(gray)
    seen = logo.on_screen(gray) if use_logo else None
    if use_logo:
        logo.check_still_valid(score, seen)
    return score, seen, rule.step(score, seen)


def replay(folder, out, use_logo):
    logo, rule = Logo(), Rule()
    with open(out, "w") as f:
        for p in sorted(pathlib.Path(folder).glob("*.jpg")):
            score, seen, muted = judge(cv2.imread(str(p)), logo, rule, use_logo)
            f.write(json.dumps({"file": p.name, "is_match": round(score, 4), "logo": seen and round(seen, 3), "muted": muted}) + "\n")
    say(f"replay done, {out}")


def live(words, out, use_logo, save=None):
    hit = find_video(words)
    if not hit:
        sys.exit("No playing video found. Is it open and playing in the Chrome on port 9224?")
    video, page = hit
    say(f"watching: {page['title'][:70]}")
    say("close this window or press Ctrl+C to stop, the sound comes back on its own")
    logo, rule, fails, was, n = Logo(), Rule(), 0, False, 0
    if save:
        pathlib.Path(save).mkdir(parents=True, exist_ok=True)
        say(f"saving every frame to {save}")
    try:
        with open(out, "a") as f:
            while fails < GIVE_UP_FAILS:
                tick = time.time()
                try:
                    shot = cdp(page, "Page.captureScreenshot", {"format": "jpeg", "quality": 72})
                    jpeg = base64.b64decode(shot["data"])
                    frame = crop_to_video(cv2.imdecode(np.frombuffer(jpeg, "uint8"), cv2.IMREAD_COLOR), video, page)
                    n += 1
                    if save:
                        cv2.imwrite(str(pathlib.Path(save, f"{n:06d}.jpg")), frame)
                    score, seen, muted = judge(frame, logo, rule, use_logo)
                    fails = 0
                except (OSError, KeyError, ValueError, websocket.WebSocketException, cv2.error) as e:
                    fails += 1
                    say(f"grab failed {fails}/{GIVE_UP_FAILS}: {str(e)[:70]}")
                    hit = find_video(words) if fails % 3 == 0 else None
                    if hit:
                        video, page = hit
                    time.sleep(EVERY_S)
                    continue
                if muted != was:
                    try:
                        set_muted(video, muted)
                        was = muted
                        say(f"{'MUTED  ' if muted else 'UNMUTED'}  match {score:.2f}  logo {'-' if seen is None else f'{seen:.2f}'}")
                    except Exception as e:
                        say(f"couldn't change the sound: {str(e)[:70]}")
                f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "file": f"{n:06d}.jpg", "is_match": round(score, 4), "logo": seen and round(seen, 3), "muted": muted}) + "\n")
                f.flush()
                time.sleep(max(0, EVERY_S - (time.time() - tick)))
        say("the video has gone, stopping")
    finally:
        try:
            set_muted(video, False)
            say("sound is back on")
        except Exception:
            say("couldn't reach the video to unmute it. If it's still silent, unmute it in the player.")


def main():
    a = argparse.ArgumentParser(description="Mutes the ads in a tennis stream in Chrome.")
    a.add_argument("--tab", help="words from the tab's title or address")
    a.add_argument("--no-logo", action="store_true", help="d1 alone, don't learn the corner logo")
    a.add_argument("--replay", help="folder of saved frames, no Chrome and no muting")
    a.add_argument("--log", help="where to write decisions")
    a.add_argument("--save", nargs="?", const=True, metavar="DIR",
                   help="keep every frame for replay later, in captures\\ unless you give a folder (about 180 MB an hour at 4K)")
    x = a.parse_args()
    (HERE / "logs").mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    save = str(HERE / "captures" / stamp) if x.save is True else x.save
    out = x.log or (str(pathlib.Path(save, "decisions.jsonl")) if save and not x.replay else str(HERE / "logs" / f"{stamp}.jsonl"))

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)  # closing the console window

    server = start_d1()
    try:
        if x.replay:
            replay(x.replay, out, not x.no_logo)
        else:
            live(x.tab, out, not x.no_logo, save)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.terminate()


if __name__ == "__main__":
    main()
