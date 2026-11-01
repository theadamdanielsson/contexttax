# contexttax

Where your Claude Code usage actually goes.

Most of it isn't loops or re-reads. It's long conversations. Every turn sends the whole conversation again, so a session at 500k tokens pays for all of it on every message.

```
uvx contexttax
```

No install with uv. Or, with plain Python 3:

```
curl -fsSL https://raw.githubusercontent.com/theadamdanielsson/contexttax/main/contexttax.py | python3 -
```

## What it shows

My last 27 days:

```
contexttax · Claude Code · last 27 days · 316 sessions · 8,188 model calls

  76% of your usage went to turns where the conversation was already over 200k tokens.
  Your biggest 10% of sessions used 90%. One session alone used 14%.
  A turn at 500k+ context costs 5.0x a turn at 50-100k.
  Median turn: 255k of context. One in ten: over 604k.

  Starting fresh with a short summary at 200k would have saved about 24%.

  What people usually blame:
    re-reading the same file     43 of 792 reads
    loops (same call 3 times)    1
    huge tool outputs            1%
    subagents                    5%
    cache rebuilt after a break  4%

  Where it goes: cache reads 70%, cache writes 20%, output 10%
```

## Private by design

It reads the logs Claude Code already keeps in `~/.claude/projects`. It prints numbers only. It sends nothing anywhere and writes nothing. One file, standard library only, so you can read all of it first.

## How it counts

Usage is weighted by API prices, the closest public stand-in for how plan limits are counted. Per model: Opus 5, Sonnet 3, Haiku 1. Within a model: input 1, cache write 1.25 (5-minute) or 2 (1-hour), cache read 0.1, output 5. Plan limits aren't published, so treat the shares as close, not exact.

The fresh-start saving assumes each turn past 200k would instead carry a 20k summary. It ignores what a summary leaves out.

## Options

```
contexttax --days 7     # last week
contexttax --days 0     # everything in the logs
contexttax --json       # the numbers as JSON
```

## Share yours

If your numbers look different from mine, I'd like to see them. Open an issue with the output.

MIT licence.
