# Making the demo video (under 3 minutes)

Everything on screen is a real run. Waiting time is sped up and labelled as such; results are never faked.

- [narration.md](narration.md): the script, scene by scene, with what's on screen. [narration.txt](narration.txt) is
  the same text for a voice generator.
- [cards/](cards/): title and end cards, and labels to place over the footage (1920×1080, labels are transparent).
- `scripts/video/presenter.py`: the animated Rewind logo that presents in the corner, driven by your voiceover.
- `scripts/video/subtitles.py`: subtitles timed to your voiceover.

## 1. Set up the screen

- Browser window at 1920×1080, zoom 110%, bookmarks bar hidden, light theme, Do Not Disturb on.
- Tabs: the GitLab issue list, the Rewind dashboard, the storefront
  (https://rewind-demo-production-twegh4u3wa-ew.a.run.app), and the GitLab merge requests.
- Recorder: QuickTime (**File → New Screen Recording**) or OBS; Screen Studio on a Mac adds smooth zooms.

## 2. Record

**A. The live run (scenes 3 and 4).** One continuous recording of about 16 minutes:

1. `python scripts/demo.py rewind --no-label` creates the flash-sale issue. Open it in GitLab.
2. Start recording. Add the `rewind:auto` label, then switch to the run on the dashboard and let it fill in.
3. Open the merge request when the review lands: Duo's "requested changes" and Rewind's policy note.
4. When the run says **Releasing**, switch to the storefront and stay there: the price goes wrong, the Rewind strip
   says it's undoing the release, then "Prices recovered" and "Restored by Rewind".
5. Back to the run page: the arc to the checkpoint, "production mitigated", zero drift. Then the closed incident.

Note the run's "production mitigated" time and put it in the narration (scene 4).

**B. Short clips of pages that already exist (scenes 1, 5, 6, 7).** 5–10 seconds each, slow scroll:

| Scene | Clip |
|---|---|
| 1 | GitLab merge requests opened by "Duo Developer" |
| 5 | MR !13: "Deviation from the acceptance criteria… queued to an in-memory outbox" |
| 5 | The run for #14 that was blocked: the finding on `app/receipts.py:13` and "How to ship it safely" |
| 6 | A `/rewind` comment and Rewind's reply on an issue; the Policy page's "Who can do what"; the Connect page |
| 7 | A run's DevSecOps lifecycle at 9 of 9 |

## 3. The voice

Paste [narration.txt](narration.txt) into a text-to-speech service (ElevenLabs sounds most natural: pick a calm voice,
stability around 50%) and download the audio. A quick draft for timing the edit, with the voice built into macOS:

```bash
say -v Samantha -r 155 -o voiceover-draft.aiff -f docs/video/narration.txt
```

## 4. The presenter and subtitles

```bash
make video-presenter AUDIO=voiceover.mp3   # docs/video/build/presenter-alpha.mov and presenter-green.mp4
make video-subtitles AUDIO=voiceover.mp3   # docs/video/build/subtitles.srt
```

Put the presenter in the bottom-right corner at about 14% of the frame width. Use `presenter-alpha.mov` in DaVinci
Resolve, Final Cut or Premiere (it's transparent); in CapCut use `presenter-green.mp4` with **Chroma key** on the green.
Labels go bottom-left, so they don't collide.

## 5. Edit

DaVinci Resolve or CapCut, 1920×1080 at 30 fps.

1. Lay the voiceover down first; it sets the timing.
2. Cut the footage to it scene by scene (see the table in [narration.md](narration.md)).
3. Speed up the waits in recording A (×20 is about right) and put `label-real-sped-up.png` over them. Keep the break
   and the rewind at real speed with `label-real-time.png`, then `label-mitigated.png` (update its number if your run
   differs: edit the text in the card and re-export, or add a text layer).
4. Title card at the start of scene 2, end card for scene 7. Presenter and subtitles on top.
5. No music, or only music you have the rights to. No third-party logos.
6. Check the length: under 3 minutes.

## 6. Publish

Upload to YouTube as **public** or **unlisted**. Title: *Rewind: Ctrl+Z for autonomous software delivery on GitLab*.
Put the dashboard and repository links in the description. Then add the link to the README (replace the `DEMO-VIDEO`
comment at the top), `make ship`, and `make release`.
