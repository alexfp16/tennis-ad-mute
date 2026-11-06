# Tennis ad mute

Mutes the ads in a tennis stream playing in Chrome, then brings the sound back when play returns. It runs locally with [Liquid AI's d1-2B](https://huggingface.co/LiquidAI/d1-2B) and a small OpenCV logo check. There's no training and no template to make.

Every 2 seconds it asks d1 one question about the current frame: "Is this part of a live tennis broadcast?"

## Setup

Tested on Windows 11 with an RTX 3060 12GB. It needs about 4 GB of VRAM.

1. Install Python 3 and run `pip install -r requirements.txt`.
2. Install a recent [llama.cpp](https://github.com/ggml-org/llama.cpp) build with `llama-server` on your PATH. I used build 12102.
3. From [LiquidAI/d1-2B-GGUF](https://huggingface.co/LiquidAI/d1-2B-GGUF), download `d1-2B-Q8_0.gguf` and `mmproj-d1-2B-F16.gguf` into this folder. The model has its own license, so check it on that page.
4. Start Chrome with remote debugging, using a separate profile:

   ```
   chrome.exe --remote-debugging-port=9224 --user-data-dir=C:\chrome-mute
   ```

   Anything on your PC can control that Chrome profile while the port is open, so don't use your main profile.

If your files live elsewhere, set `LLAMA_SERVER`, `D1_MODEL` and `D1_MMPROJ`.

## Run it

1. Open the match in that Chrome, in a visible window.
2. Double-click `mute_ads.bat`, or run `python mute_ads.py`. Add `--tab tennis` if two videos are playing.
3. Close the window when you're done. The sound comes back.

d1 loads on its own and stops with the tool. If a server is already on port 10098, the tool uses it and leaves it running.

## How it decides

| Situation | What happens |
|---|---|
| Low score, no logo on screen | mutes at once |
| Low score, logo on screen | stays on, so close-ups of players and umpires are safe |
| Logo not learned yet | mutes after 2 low frames |
| 5 match frames in a row | sound comes back, about 10 seconds |

Muting fast and unmuting slowly is deliberate: a loud ad is worse than a few lost seconds of commentary.

It learns the corner logo itself during the first few minutes of play. It relearns if the window changes shape or the logo changes. If a channel keeps its logo on during ads, use `--no-logo`.

## Results from my tests

| Test | Ads caught | Match sound lost |
|---|---|---|
| Recorded match, 28 min, 4 ad breaks | about 97% | less than a minute |
| Frames from a live match, 2 ads | about 99% | about a minute |
| Clean feed with no ads, 21 min | none to catch | 0 s |

I tuned this on the same footage I scored it on, and the live number has only two ads in it, so treat these as a working tool and not a benchmark.

## Other sports

Change `STATE` and `QUESTIONS` near the top of `mute_ads.py`. I haven't tested this on other sports, so let me know how it goes.

## Options

- `--tab WORDS`: pick the video by words in the tab's title or address
- `--no-logo`: d1 alone
- `--save [DIR]`: keep every frame, in `captures\<time>\` unless you give a folder, about 180 MB an hour at 4K. Decisions go in the same folder as `decisions.jsonl`. Off by default.
- `--replay DIR`: run over saved JPEG frames with no Chrome and no muting
- `--log FILE`: where decisions go, `logs\` by default

The thresholds are near the top of `mute_ads.py`: `MUTE_AFTER_WITH_LOGO`, `MUTE_AFTER_NO_LOGO`, `UNMUTE_AFTER`, `LOW`.

## Privacy

By default every frame is looked at once and thrown away, and nothing leaves your PC. Saved frames are copies of a broadcast, so keep them private and out of any repo.

## Limits

- DRM video captures as black, so it can't work there. Don't try to get around that. Use it only on streams you're allowed to watch.
- Tennis promos can look like play, and they can get through.
- A shot with no court in it, such as an umpire close-up, can cause a mute when no logo is on screen.
- If the tool is force-killed, the video stays muted. Unmute it in the player.
- It crops each frame to the video before asking d1. If it can't find the video's position, it falls back to the whole window, and d1 scores a small picture much lower.

## Updates

I'll note changes here as I test on more matches.
