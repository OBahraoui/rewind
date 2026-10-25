# Making the demo video (under 3 minutes)

Everything on screen is a real run. Waiting is sped up and labelled; results are never faked. You need a MacBook,
about 30 minutes of recording, and an hour of editing.

**In this folder:** [narration.md](narration.md) (script and what's on screen, scene by scene),
[narration.txt](narration.txt) (the same text for a voice generator), [cards/](cards/) (title, end card and labels).
**Tools in the repo:** `make video-presenter` (the animated Rewind presenter) and `make video-subtitles`.
**Free apps:** QuickTime (built in), [CapCut desktop](https://www.capcut.com) for editing, a text-to-speech voice such
as [ElevenLabs](https://elevenlabs.io).

## 0. Order of work

1. Record the live run (step 2A) and note its "production mitigated" time.
2. Put that number into `narration.txt` (scene 4: "in 17 seconds") and `cards/label-mitigated.png` (or a text layer).
3. Record the short clips (step 2B).
4. Make the voiceover (step 3), then the presenter and subtitles (step 4).
5. Edit (step 5), export, upload (step 6).

## 1. Set up the Mac

- **Focus:** Control Center → Focus → Do Not Disturb, so no notifications appear.
- **Browser:** Chrome, light theme. View → uncheck "Always Show Bookmarks Bar". Zoom 110% (Cmd + =).
- **Full screen:** View → Enter Full Screen (Ctrl + Cmd + F). It hides the menu bar and Dock.
- **Tabs, in this order** (switch with Cmd + 1…4, no mouse needed):
  1. GitLab: the issue list
  2. the Rewind dashboard: https://rewind-twegh4u3wa-ew.a.run.app
  3. GitLab: the merge requests list
  4. the storefront: https://rewind-demo-production-twegh4u3wa-ew.a.run.app
- **Recorder:** press **Cmd + Shift + 5**, choose **Record Entire Screen**, open **Options**: Save to Desktop,
  Microphone **None**, **Show Mouse Clicks** on. Click **Record**. To stop: the stop button in the menu bar, or
  Cmd + Ctrl + Esc. The recording appears on the Desktop as a .mov.

## 2. Record

### A. The live run (scenes 3 and 4): one recording, about 20 minutes

In the terminal (not recorded): `python scripts/demo.py rewind --no-label`. It prints the new issue's link.

Start recording, then:

1. **Tab 1**, open the new issue. Add the label `rewind:auto` (Labels → rewind:auto). Hold 3 seconds.
2. **Tab 2**, open the run (the newest on the overview). Leave it on screen: the track fills in by itself.
3. When the run says **Merging**, **tab 3**: open the merge request, scroll to Duo's review ("requested changes")
   and Rewind's note ("Everything this change ships can be undone, so by Rewind's policy…"). Hold 5 seconds.
4. When the run says **Releasing**, **tab 4**, the storefront, and **don't touch anything**: the strip says
   "Rewind is watching this release", prices go wrong, it turns to "Rewind is undoing this release", then
   "Prices recovered" and "Restored by Rewind". This part plays at real speed in the video.
5. **Tab 2**, the run page: the arc back to the checkpoint, "Production mitigated", "Zero drift". Scroll slowly to the
   undo steps. Then click "Incident #…" in the header and show that it's closed.
6. Stop recording.

### B. Short clips (scenes 1, 5, 6, 7): 5–10 seconds each, slow scroll

| Scene | Record |
|---|---|
| 1 | GitLab merge requests list: MRs authored by "Duo Developer" |
| 5 | MR !13: its description, "Deviation from the acceptance criteria… queued to an in-memory outbox" |
| 5 | The dashboard run for #14 that was **blocked**: the gate finding on `app/receipts.py:13` and "How to ship it safely" |
| 6 | An issue with a `/rewind` comment and Rewind's reply; the dashboard's **Policy** page ("Who can do what"); **Connect** |
| 7 | Any run page scrolled to the DevSecOps lifecycle (9 of 9); the **How it works** diagram |

One recording with pauses between clips is fine: you'll cut them apart in the editor.

## 3. The voiceover

1. On elevenlabs.io: Text to Speech → paste [narration.txt](narration.txt) → choose a calm voice →
   settings: Stability about 50%, Similarity about 75% → Generate → listen → Download (MP3).
   It should land around 2:30–2:45. If it's longer than 2:50, raise the speed slightly or regenerate.
2. Free plan: check the plan's terms; credit the voice in the YouTube description if it asks for attribution.
3. Quick draft to rehearse the edit, with the Mac's own voice:
   `say -v Samantha -r 155 -o voiceover-draft.aiff -f docs/video/narration.txt`

Save the final file as `voiceover.mp3` in the repo folder.

## 4. The presenter and subtitles

```bash
make video-presenter AUDIO=voiceover.mp3   # docs/video/build/presenter-green.mp4 and presenter-alpha.mov
make video-subtitles AUDIO=voiceover.mp3   # docs/video/build/subtitles.srt
```

## 5. Edit in CapCut (desktop)

1. **New project.** Canvas: **16:9**, 1920×1080 (click "Ratio" above the preview).
2. **Import** (drag into the media panel): `voiceover.mp3`, your recordings from the Desktop,
   `docs/video/cards/*.png`, `docs/video/build/presenter-green.mp4`.
3. **Voice first:** drag `voiceover.mp3` to the timeline. It sets every time below.
4. **Footage, scene by scene,** on the main video track, following [narration.md](narration.md):
   - Scene 1 (0:00–0:14): the merge requests clip.
   - Scene 2 (0:14–0:24): `cards/title.png`, stretched to 10 seconds.
   - Scene 3 (0:24–1:04): recording A from the label to the merge request. Select the waiting parts → **Speed** →
     about ×20. Split with **Cmd + B** to keep the key moments (label added, merge request opened, review, policy
     note) at normal speed.
   - Scene 4 (1:04–1:44): recording A from "Releasing": storefront at **real speed**, then the run page and incident.
   - Scenes 5–6: the clips from B, trimmed to the voice.
   - Scene 7 (2:36–2:50): the lifecycle clip, then `cards/end.png` for the last 6 seconds.
   - If a clip doesn't fill the 16:9 frame (MacBook screens are taller), select it → Basic → **Scale** up slightly so
     it fills, and reposition.
5. **Labels:** drag `cards/label-real-sped-up.png` onto a track above the sped-up parts, `label-real-time.png` over the
   storefront part, `label-mitigated.png` right after the rewind, `label-policy-ships.png` over the policy note,
   `label-agent-outbox.png` and `label-gate-blocked.png` in scene 5. They're full-frame and transparent: no
   positioning needed.
6. **Presenter:** drag `presenter-green.mp4` onto a track above everything, starting at 0:00. Basic → Scale about
   18%, position bottom-right (keep 40 px from the edges). **Cutout → Chroma key** → pick the green → Intensity
   about 30. Mute its audio (the voice track already plays). Hide it during the title and end cards if you prefer.
7. **Subtitles:** Text → **Local captions** → import `docs/video/build/subtitles.srt` (or **Auto captions** from the
   voice). Style: white, semi-bold, size about 6, a dark background box, bottom centre.
8. **No music** (or only music you have rights to). No third-party logos.
9. **Check:** watch it once start to finish with sound. Total under **3:00**.
10. **Export:** 1080p, 30 fps, H.264, MP4.

*Using DaVinci Resolve instead:* import `presenter-alpha.mov` (already transparent, no chroma key) and the .srt
(File → Import → Subtitle).

## 6. Publish

1. YouTube Studio → Create → Upload video.
   - Title: *Rewind: Ctrl+Z for autonomous software delivery on GitLab*.
   - Description: one line on what Rewind does, the dashboard link, the repository link.
   - Audience: not made for kids. Visibility: **Public** or **Unlisted**.
2. Copy the link. In `README.md`, replace the `DEMO-VIDEO` comment line at the top with
   `· <a href="https://youtu.be/...">Demo video</a>`.
3. `make ship`, then `make release`, then submit on Devpost with `docs/brand/rewind-thumbnail.png` as the thumbnail.
