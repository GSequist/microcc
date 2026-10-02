# Vendored from github.com/ledbetterljoshua/functional-emotions-video (MIT — see ../../LICENSE-lib).
# Manual, one-off script — edit the hardcoded path below for your song, then run from the project root.
# See ../../TIMING.md for the full pipeline order and why this is opt-in (heavy ML deps).
import librosa, numpy as np, json
y, sr = librosa.load("assets/functional-emotions.mp3", sr=22050, mono=True)  # EDIT: your song path
hop = 735  # 30 fps at 22050
FPS = sr/hop
S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
def band(lo,hi):
    b = S[(freqs>=lo)&(freqs<hi)].mean(0); return b/ (np.percentile(b,99)+1e-9)
rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]; rms/=np.percentile(rms,99)
onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop); onset/=np.percentile(onset,99)
tempo, beats = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop)
bt = librosa.frames_to_time(beats, sr=sr, hop_length=hop)
cent = librosa.feature.spectral_centroid(S=S, sr=sr)[0]; cent/=np.percentile(cent,99)
# harmonic/percussive
H,P = librosa.decompose.hpss(S)
perc = P.mean(0); perc/=np.percentile(perc,99)
harm = H.mean(0); harm/=np.percentile(harm,99)
chroma = librosa.feature.chroma_stft(S=S**2, sr=sr)
# structural boundaries
mf = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
bounds = librosa.segment.agglomerative(np.vstack([mf, chroma]), 16)
bt_b = librosa.frames_to_time(bounds, sr=sr, hop_length=hop)
r3 = lambda a: [round(float(v),3) for v in np.clip(a,0,1.5)]
out = dict(fps=FPS, n=len(rms), tempo=float(np.atleast_1d(tempo)[0]), beats=[round(float(b),3) for b in bt],
  rms=r3(rms), onset=r3(onset), low=r3(band(20,150)), mid=r3(band(150,2000)), high=r3(band(2000,11000)),
  cent=r3(cent), perc=r3(perc), harm=r3(harm), chroma=[r3(c/ (c.max()+1e-9)) for c in chroma.T[:, :]][:0],
  chromaArgmax=[int(i) for i in chroma.argmax(0)], sections=[round(float(b),2) for b in bt_b])
json.dump(out, open("analysis/features.json","w"))
print("tempo", out["tempo"], "beats", len(bt), "frames", len(rms))
print("sections", out["sections"])
# coarse energy per 5s
for i in range(0, len(rms), int(FPS*5)):
    print(f"{i/FPS:6.1f}s rms {rms[i:i+int(FPS*5)].mean():.2f} low {band(20,150)[i:i+int(FPS*5)].mean():.2f} perc {perc[i:i+int(FPS*5)].mean():.2f} cent {cent[i:i+int(FPS*5)].mean():.2f}")
