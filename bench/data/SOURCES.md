# Where the evaluation corpus comes from

`detection_corpus.jsonl` is built by `bench/fetch_corpus.py` and committed, so a number
measured today can be compared with one measured six months from now. Every row carries the
`source` it came from.

| Source | Licence | Used for | Cap |
|---|---|---|---|
| [`deepset/prompt-injections`](https://huggingface.co/datasets/deepset/prompt-injections) | Apache-2.0 | injections and benign questions, several languages | 300 per class |
| [`jackhhao/jailbreak-classification`](https://huggingface.co/datasets/jackhhao/jailbreak-classification) | Apache-2.0 | long roleplay jailbreaks, and benign roleplay prompts | 300 per class |
| `security_adjacent.jsonl` | this repository | benign prompts that look hostile | all |

Both public datasets are Apache-2.0, which permits redistributing a subset with attribution.
Sampling is seeded (`SEED` in the fetch script) and applied after a shuffle, so the caps are
a sample rather than a prefix and re-running the script reproduces the same file.

## What the public sets are, and are not

They are small and noisy, and that is the state of this benchmark landscape rather than a
choice made here. `deepset/prompt-injections` is 662 rows. `jackhhao/jailbreak-classification`
is mostly long roleplay jailbreaks of one family, which a detector can learn to spot by
length alone. Neither contains the case that actually matters in production: a prompt that is
*about* injection without being one.

## security_adjacent.jsonl

Written by hand for this project, because nothing published covers it. Seventy benign prompts,
each one close enough to a rule to trip it:

| Category | What it is |
|---|---|
| `security_work` | A security engineer doing their job: writing test cases, reviewing a regex, drafting policy. |
| `logs_and_traces` | A developer pasting a log line, a transcript or an audit event that quotes an attack. |
| `building_with_llms` | Ordinary questions about system prompts, chat templates and delimiters. |
| `ordinary_roleplay` | "Act as an interviewer", "you are a helpful assistant" - the persona shape, used honestly. |
| `ordinary_correction` | "Ignore the previous draft", "disregard my earlier message" - the override shape, used honestly. |
| `innocent_homonym` | "Developer mode" in Chrome, "jailbreaking" a phone, "danger" containing DAN. |
| `encoded_content` | Base64, a JWT header, a hex digest: the shapes an evasion rule looks for. |
| `legitimate_exfil_shape` | "Post this to our webhook", "upload the export" - real integrations. |
| `credential_questions` | "Where do I find my API key" - asking about credentials without asking for one. |

The false positive rate on this set is the number in the README that nobody else publishes,
and it is reported per category, because a detector that is wrong about one category is fixable
and a detector that is wrong about all of them is not.
