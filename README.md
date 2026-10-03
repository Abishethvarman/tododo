# Tododo

A local-first to-do app for people whose lists are made of big tasks with subtasks, many of which repeat.

Type a sentence the way you'd say it:

> Clean the apartment every Saturday: kitchen, bathroom, laundry

and it becomes a task, three subtasks and a weekly repeat. When the cycle passes, the subtasks uncheck themselves and the next due date is set, so nothing needs retyping.

## How the open-source AI is used

- **Gemma (open-weight) via [Ollama](https://ollama.com)** reads the sentence and returns structured JSON: title, subtasks, repeat rule.
- **Optional "Suggest subtasks"** asks the same local model to break a vague task into steps.
- **Offline fallback:** if Ollama isn't running, a rule-based parser handles the common phrasings, so the app always works.
- You always **review and edit** what the model understood before it is saved.

Nothing leaves the machine: no accounts, no cloud API, no per-use cost.

## Run it

Requires Python 3.9+ (standard library only, nothing to install).

```bash
# optional but recommended: the local model
ollama pull gemma3

python app.py
# open http://localhost:8000
```

Settings (environment variables): `OLLAMA_MODEL` (default `gemma3`), `OLLAMA_URL` (default `http://localhost:11434`), `PORT` (default `8000`), `TODO_DB` (path of the SQLite file).

Swapping models is one variable, e.g. `OLLAMA_MODEL=llama3.2 python app.py`.

## Features

- Nested subtasks with progress bars; a task completes when all its subtasks are done
- Repeats: daily, weekly (any days), monthly
- Repeating tasks auto-reset when their cycle passes
- Review-before-save step after natural-language parsing
- JSON export (`/api/export`)
- Dark mode

## Files

- `app.py`: server, SQLite storage, repeat logic, parsing
- `index.html`: the whole UI
