# AI advisor

For items FSU cannot decide on its own, you can ask a language model running
**on your own computer** for a second opinion:

```bash
fsu scan --ai
```

It is **off by default** and **advice only**.

## What it can and cannot do

| The AI… | |
|---|---|
| gives an opinion on list-only and medium items | ✔ |
| can lower a medium item to list-only when it judges it to be in use (confidence ≥ 0.6) | ✔ |
| can raise any item to a higher band | ✘ never |
| is consulted about items with concrete evidence (≥ 0.70) | ✘ never |
| decides what gets moved | ✘ never — you do |

Because it can only make FSU *more* careful, even a wrong or manipulated
answer cannot cause anything to be moved. (Folder names come from the file
system and could be crafted to trick a model; this design makes that useless.)

Opinions look like this:

```
AI: in_use (0.90) - The directory contains steamrt3/ and steamrt4/ subfolders, indicating it is part of the Steam runtime environment…
AI: uncertain - Only a weak name similarity to a known app is found; no evidence that the app is installed or removed.
```

`uncertain` is a good, honest answer: it means the available data does not
decide the question.

## Setting it up

1. Install [Ollama](https://ollama.com) and start it.
2. Download a local model, for example:

   ```bash
   ollama pull gpt-oss:20b     # ~13 GB download; runs well on a 16 GB GPU
   ```

   Any local text model works; FSU uses the one you choose with
   `--model NAME`, or picks an installed one automatically.
3. Run `fsu scan --ai --model gpt-oss:20b`.

FSU never downloads models and never starts the Ollama service. If Ollama is
not available, the scan simply continues without opinions.

## Privacy

- **Only this machine.** FSU accepts only loopback addresses
  (`localhost`, `127.0.0.1`, `::1`) and ignores system proxies.
- **No cloud models.** Ollama can list "cloud" models (such as
  `gpt-oss:120b-cloud`) that actually run on remote servers. FSU refuses them,
  because your folder names would leave your computer.
- **Names, not contents.** The model receives item names, their location,
  the names of the files directly inside them and FSU's positive findings —
  never the contents of any file.

## What the model sees — and why so little

Real tests showed that models over-interpret weak signals. Each of the
following was observed and is therefore **not** sent:

| Not sent | What went wrong when it was |
|---|---|
| FSU's score | "low heuristic score" was read as evidence of a residue, when it only means "no evidence" |
| Size | "large cache, so the app is active" |
| Dates | "written two days ago, so it is in use" / "old, so it is a residue" — even when told dates prove neither |
| Negative findings ("no known match", "no program with this name") | absence was taken as proof of removal; an in-use 3.6 GB game runtime was called a residue |
| Examples of software in the prompt | the model answered "explicitly listed as a tool that may be in use" instead of looking at the data |

What it does receive are positive findings with their strength spelled out,
for example `weak name similarity (43%) to installed app 'HytaleLauncher'`
(a bare "0.89" was once read as a version number).

Traces of the same application are judged together in one request — the same
name in several places (`~/.cache/x` and `~/.config/x`) and a reverse-DNS
name with its plain form (`com.example.app` and `app`) — so they always get
the same opinion. Answers are reproducible (temperature 0, fixed seed).

## How good is it?

On a real Fedora system, across three local models (llama3 8B, gemma4 12B,
gpt-oss 20B), the AI was right on roughly half of the ambiguous items. With
the neutral input described above, gpt-oss:20b made **no wrong calls** on the
items whose status was known — it answered `uncertain` when the data did not
decide, and `in_use` when it recognised names or structures.

So: treat the AI as a knowledgeable friend who recognises names like
`steamrt3` or `RustDesk`, not as an authority. The decision stays yours.
