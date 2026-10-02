# Vendored from github.com/ledbetterljoshua/functional-emotions-video (MIT — see ../../LICENSE-lib).
# Manual, one-off script — edit the hardcoded path below (Demucs' output for your song), run after
# `demucs --two-stems=vocals <song>`. mlx-whisper is Apple Silicon; swap for openai-whisper elsewhere.
# See ../../TIMING.md.
import mlx_whisper, json, numpy as np, soundfile as sf, librosa
v, sr = sf.read("analysis/stems/htdemucs/functional-emotions/vocals.wav")  # EDIT: your Demucs output path
v = v.mean(1) if v.ndim>1 else v
v16 = librosa.resample(v.astype(np.float32), orig_sr=sr, target_sr=16000)
# vocal activity per 0.5s
w=8000; act=[float(np.sqrt((v16[i:i+w]**2).mean())) for i in range(0,len(v16),w)]
mx=np.percentile(act,95); print("activity:", "".join("#" if a>0.15*mx else ("." if a>0.04*mx else " ") for a in act))
words=[]
for start in range(0, int(len(v16)/16000), 25):
    seg = v16[start*16000:(start+30)*16000]
    r = mlx_whisper.transcribe(seg, path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        word_timestamps=True, language="en", condition_on_previous_text=False,
        no_speech_threshold=0.9, hallucination_silence_threshold=2.0)
    for s in r["segments"]:
        for wd in s.get("words",[]):
            t0=wd["start"]+start
            if t0 < start+ (27.5 if start>0 else 0) and start>0 and t0 < start+2.5: continue  # overlap handled below
            words.append(dict(w=wd["word"].strip(), s=round(t0,2), e=round(wd["end"]+start,2), p=round(wd.get("probability",0),2), chunk=start))
    print(start, " ".join(w["word"].strip() for s in r["segments"] for w in s.get("words",[])))
json.dump(dict(words=words, activity=act), open("analysis/words_raw.json","w"))
