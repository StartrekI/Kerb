<div align="center">

# Kerb

**Type anything — bakeries in Leeds, tattoo studios in Berlin — and Kerb scrapes Google Maps for it,
then shows the evidence behind every score.**
No API key. No browser. No other scraper. And when it can't measure something, it says so.

[![License: MIT](https://img.shields.io/badge/license-MIT-000000.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-172%20passing-2C6A4F.svg)](tests/)
[![Dependencies](https://img.shields.io/badge/dependencies-2-8A5E23.svg)](pyproject.toml)
[![Self-hosted](https://img.shields.io/badge/data-never%20leaves%20your%20machine-19C9E6.svg)](#legal)

![The findings screen](docs/findings.png)

</div>

---

Every Maps scraper hands you a spreadsheet. You still have to spend the afternoon
working out which of the 400 rows are worth a phone call — which ones are actually
in the trade, which closed in 2023, which are branches of a chain that already has
an agency.

Kerb is that afternoon, written down as a specification and executed in seconds,
**with the reasoning kept**.

```
  96.7  Mayvn Cafe               no website · cafe · excellent rating      4.9
  94.7  High Ground              social page only · cafe · excellent       4.8
  93.3  Blank Street Coffee      booking page only · cafe · excellent      4.6
```

Those chips are not decoration. They are the measurements that produced the score,
on the row, before you click anything.

---

## Three outcomes, not two

This is the part that makes Kerb different, so it goes first.

Most tools have two buckets: it matched, or it didn't. That is a lie by omission,
because a website check that *timed out* is not the same as a business with no
website — and if you conflate them, you will call people on the strength of a
network error.

| Outcome | What it means | What Kerb shows you |
|---|---|---|
| **Qualified** | Measured, and it passes every condition you set | The score, and every signal that contributed to it |
| **Rejected** | Measured, and it fails at least one condition | **Which** condition, with the value that failed it |
| **Never found out** | Kerb could not take the measurement | Said plainly, in its own tab, in amber — never counted as a rejection |

That third row is the entire architecture. A failed measurement is never a verdict
about a business.

> Kerb would rather tell you it doesn't know than quietly make something up.

---

## Where do I start?

| If you want to… | Go here |
|---|---|
| Install it and get collecting | [Setup](#setup) |
| Scrape Google Maps for a trade | [Your first run](#your-first-run) |
| Qualify a CSV you already have | [From a file you already have](#from-a-file-you-already-have) |
| Understand what it measures | [What it measures](#what-it-measures) |
| Write a brief by hand and schedule it | [The brief](#the-brief) |
| Know why reviews are missing | [Signed out vs signed in](#signed-out-vs-signed-in--read-this-bit) |
| Scrape review text | [Reviews — opt-in](#reviews--opt-in-and-the-one-place-a-browser-is-needed) |
| Not get blocked | [Staying unblocked](#staying-unblocked) |
| Know what it *can't* do | [What Kerb does not do](#what-kerb-does-not-do) |

---

## Setup

Three commands, and the third one is the app.

```bash
pip install "kerb[server]"
kerb setup
kerb
```

No Docker. No Chrome. No chromedriver. No API key. No account.
Two dependencies: `httpx` and `PyYAML`.

### What `kerb setup` actually does

It builds a **browsing profile** — a persistent identity Kerb collects with, so
a thousand requests look like one person rather than a thousand strangers. It
stores a user agent, a locale, Google's consent choice, and any cookies you give
it, at `~/.kerb/session.json`, mode `0600`.

Then it **proves the profile works before saving it**. A setup that writes a file
and declares success leaves the first real run to discover the problem, forty
places in.

```
$ kerb setup --gl uk

saved  ~/.kerb/session.json
       3 cookie(s), locale en/uk, created 0s ago
       signed in to Google: NO -- Google serves a reduced view of Maps
```

> **`--gl` matters more than it looks.** It decides which Google you get.
> `--gl uk` and `--gl us` return different businesses for the same query.

### Signed out vs signed in — read this bit

This is the one thing that will confuse you if nobody says it:

> **Google serves signed-out clients a reduced view of Maps.**

Signed out, everything below still arrives in full:

| Collected signed out | |
|---|---|
| ✅ name, category, address | ✅ phone, website |
| ✅ rating, coordinates | ✅ the Google CID (identity) |
| ❌ **review counts** | |

That last row is not cosmetic. Kerb's `reviews` condition is on by default at
`>= 30`, and with no counts every business lands in **never found out** — correct,
because Kerb will not invent a measurement, but useless as a filter. The UI
notices and offers `rating_band` instead, which *is* collected.

**To lift the limit, hand Kerb a signed-in session — once.**

<details>
<summary><b>How to export your Google cookies</b> (click to open)</summary>

Kerb never asks for a password and never opens a browser. You export cookies
from a browser that is *already* signed in, and hand them over once.

**Option A — a cookie-export extension (easiest)**

1. Sign in to Google in your normal browser.
2. Install any "cookies.txt" export extension.
3. Visit `google.com`, export, save the file.
4. `kerb setup --import-cookies ~/Downloads/cookies.txt`

**Option B — DevTools, no extension**

1. Sign in to Google, open `google.com`.
2. DevTools → **Network** → click any request → **Copy → Copy as cURL**.
3. Paste into a file and keep only the `Cookie:` line:
   ```
   Cookie: SID=...; HSID=...; SSID=...; APISID=...; SAPISID=...
   ```
4. `kerb setup --import-cookies that-file.txt`

Kerb accepts all three shapes browsers export — Netscape `cookies.txt`, a JSON
array, or a raw `Cookie:` header — because being told "wrong format" is a
terrible first experience.

```
$ kerb setup --import-cookies ~/Downloads/cookies.txt

read 6 cookie(s) from cookies.txt: APISID, HSID, SAPISID, SID, SSID, __Secure-1PSID
saved  ~/.kerb/session.json
       signed in to Google: yes

Signed in. You get the full Maps view.
```

Only cookie **names** are ever printed. A cookie value is a credential and does
not belong in a terminal, a scrollback buffer, or a screenshot of one.

</details>

**Check it any time:**

```bash
kerb setup --check
```

```
profile: ~/.kerb/session.json
         6 cookie(s), locale en/uk, created 2d ago
         signed in to Google: yes

working, signed in. `kerb serve` and pick Google Maps.
```

If a run ever comes back thinner than you expect, this is the first thing to run.
Kerb also drops a note into the run itself rather than letting you wonder.

### Why cookies and not a Chrome profile?

Because a browser is the thing that breaks.

The tool Kerb replaces drove a real Chrome through a driver and kept a profile
directory. It worked — and every crashed run left a `chromedriver` and its Chrome
children behind. Enough of them filled a disk. Kerb launches **no process at
all**, so there is nothing to leak, nothing to install, and nothing to keep
updated. The cookies carry the same session the profile did.

---

## Your first run

### From Google Maps

Pick **Google Maps** as the source, type any trade, list your places, press Run.
Or headless:

```yaml
# cafes.yaml
name: cafes-islington
sources: [{id: gmaps}]
where:  {mode: paste, places: [Islington London]}
what:
  trade: cafes
  keywords: [cafe, coffee, espresso, roastery]   # Google knows these are
  categories: [cafe, coffee, espresso, roastery] # synonyms; a matcher doesn't
filters:
  - {signal: trade_match, op: "!=", value: false}
  - {signal: rating_band, op: in, value: [excellent, good]}
limits: {max_results: 200}
```

```bash
$ kerb run cafes.yaml --out leads.csv

  10 found, 10 unique, 9 qualified
    trade_match      rejected 1
  1.5s
  -> leads.csv (9 rows, csv)
```

### From a file you already have

Already scraped Maps with something else? Kerb qualifies any CSV, JSON or JSONL:

```yaml
sources: [{id: csv, options: {path: exported.csv}}]
```

It auto-detects the column layout and tells you what it mapped and what it
didn't. The Google CID is the identity key, so businesses collected by Kerb and
imported from a file **deduplicate against each other for free**.

---

## Why not just use a scraper?

| A scraper gives you | Kerb gives you |
|---|---|
| 400 rows | 38 rows worth calling, and the 362 reasons |
| A name and a phone number | The measurement behind every score, with a confidence from 0 to 1 |
| Silence when a source fails | *"3 of 50 towns unreachable"* — before you act on a short list |
| Zero when it couldn't check | **"never found out"**, in its own bucket |
| The same 400 rows next week | Suppression: anyone you've dealt with is gone before a request is spent |
| A fixed set of fields | 14 signals, each with a cost tier — cheap tests run first, so most businesses are decided before anything slow does |

---

## The four screens

<table>
<tr>
<td width="50%"><img src="docs/home.png" alt="Home"><br><b>Home</b> — what it does, your recent runs, and the five steps every business goes through.</td>
<td width="50%"><img src="docs/brief.png" alt="Brief"><br><b>The brief</b> — every condition, generated from the signal registry. Reads itself back to you as a sentence.</td>
</tr>
<tr>
<td><img src="docs/run.png" alt="Run"><br><b>The run</b> — live log, stage ladder, and an honest <i>skipped · none enabled</i> for tiers that never ran.</td>
<td><img src="docs/findings.png" alt="Findings"><br><b>Findings</b> — evidence on the row, a funnel, a score distribution, and sliders that re-rank without re-running.</td>
</tr>
</table>

---

## What it measures

Fourteen signals, in **cost tiers**. Free ones read data already in hand. Paid ones
spend a network request per business and **stay off until you turn them on** —
Kerb never spends on your behalf.

The cleverness is the ordering: after each tier, Kerb tests your conditions against
what has been measured *so far*. The moment a business fails one, it stops. Most
never reach a paid tier at all.

<details>
<summary><b>All 14 signals</b> (click to open)</summary>

| Signal | Cost | What it asks | Values |
|---|---|---|---|
| `web_presence` | free | What kind of web presence is this, really? | none, social_only, booking_only, builder, owned_domain, unknown |
| `trade_match` | free | Does the category or name really place it in the target trade? | true / false |
| `reviews` | free | How many reviews does the listing claim? | number |
| `rating_band` | free | Which end of the market — with volume taken into account | excellent, good, mixed, poor, unrated |
| `liveness` | free | Is it still trading? | open, temp_closed, perm_closed, stale, unknown |
| `chain_size` | free | How many locations of this brand are in the dataset? | number |
| `contactable` | free | Is there *any* way to reach this business? | true / false |
| `review_velocity` | free | Reviews per year — growing, or established and quiet? | number |
| `review_integrity` | free | Is the review data complete, or truncated? | complete, truncated, unavailable |
| `establishment_age` | free | Oldest-review year, as a listing-age proxy | number |
| `name_script` | free | The dominant writing system of the name | latin, cyrillic, arabic, han, … |
| `site_status` | **cheap** | Does the website actually load? | live, dead, parked, placeholder, no_site |
| `site_platform` | **cheap** | What is it built on, read from the HTML | wix, squarespace, shopify, wordpress, … |
| `site_contact` | **cheap** | Contact details found on the site | text |

</details>

Each measurement is a `(value, confidence, evidence)` triple, and all three survive
into the dossier:

```
trade_match    dentist    conf 0.95    +25
   matched_category: Dentist
   rule: Dentist
   pack: trades/dentist@1
```

---

## Anything, anywhere

Six curated trade packs ship with Kerb — Dentist, HVAC, Medical, Painting, Roofing,
Vet — each a hand-written list of categories, name keywords and vetoes. They are
good. They are also six, and six is not the world.

**So type your own.** `hospitals`, `bakeries`, `tattoo studios`, `carpenters`.
Kerb stems it, matches it against each listing's category first and its name second,
and asks OpenStreetMap for the matching tag if that's your source.

```yaml
what: {trade: tattoo studios}
```

A curated pack is still better — it knows a *dental laboratory* is not a dentist.
A typed trade has no veto list, so check the **Rejected** tab after a run. The UI
says so, too.

---

## Reviews — opt-in, and the one place a browser is needed

Everything above runs over plain HTTP with no browser at all. Reviews are the
exception, and they are a **separate, deliberate step**:

```bash
kerb run brief.yaml          # no browser, no reviews
kerb reviews <run-id>        # only if you actually want them
```

Running it over a finished run has a useful consequence: reviews are harvested
for the businesses that **qualified**, not for everything discovery happened to
find. On a 2,000-business run that is usually a few dozen, not two thousand.

### Why a browser is unavoidable here

Kerb collects listings without one because the Maps search endpoint answers
plain HTTP. Reviews do not. They arrive through an internal RPC whose
`x-maps-bgbind` and `x-maps-bgkey` headers are **session-scoped and cannot be
synthesised** — replaying without them gets a valid-looking request rejected.

So the browser is used to *take a copy of one real request*, and then closed out
of the loop. Everything after the capture is a plain fetch chain.

**The browser is a key-cutter, not a scraper.** That distinction is what keeps
it fast: scrolling the review pane makes Maps render every review as a DOM node,
so pages that arrive every ~0.9s early take ~6s by page 100. Chaining the cursor
by hand renders nothing and pages arrive at a flat **~220ms** — roughly **80
seconds against 35–45 minutes** on a 3,650-review business.

### Setting it up — two things, once each

**1. Install the browser extra**

```bash
pip install "kerb[browser]"
```

The core stays at two dependencies. This is only pulled in if you ask for it,
and it is imported only at the moment a browser is actually opened — a
pure-HTTP run never loads it.

**2. Sign the profile in**

This is the part that catches everyone. **Google serves signed-out clients a
Maps page with no Reviews tab at all** — the rating is there, the reviews are
not. A browser does not fix that; being signed in does.

```bash
kerb setup --import-cookies ~/Downloads/cookies.txt
kerb setup --check
```

```
profile: ~/.kerb/session.json
         6 cookie(s), locale en/uk, created 2d ago
         signed in to Google: yes

working, signed in. `kerb serve` and pick Google Maps.
```

See [Signed out vs signed in](#signed-out-vs-signed-in--read-this-bit) for the
two ways to export cookies. Kerb never asks for a password and never opens a
sign-in form.

If you skip this step, Kerb tells you exactly what happened rather than
returning an empty list:

```
Google served the signed-out view of this place: rating shown,
no Reviews tab, no count. Sign the profile in once with
`kerb setup --import-cookies FILE`.
```

### Harvesting

```bash
kerb reviews 88c16e73a314                      # qualified businesses
kerb reviews 88c16e73a314 --max-reviews 200    # cap per business
kerb reviews 88c16e73a314 --limit 20           # only the first 20 businesses
kerb reviews 88c16e73a314 --all                # every business, not just qualified
```

Output is JSONL, one review per line:

```json
{"id": "Ch…", "reviewer": "…", "rating": 5, "text": "…",
 "relative_date": "a week ago", "timestamp_us": 1723…, "owner_reply": null,
 "photos": 2, "cid": "0x487…:0x3ce…", "business": "Edgbaston Dental Centre"}
```

One browser is opened for the whole command and closed when it finishes —
`kerb stop` closes it too. Nothing is left behind.

### Review counts without the reviews

If you only want the **number**, there is a lighter path: `reviews_live` is an
EXPENSIVE-tier signal, so it runs only on businesses that already passed every
cheaper condition. Enable it in a brief and it opens one page per surviving
business rather than per discovered one — on a 2,000-business run that is about
a minute instead of an hour.

---

## The brief

The UI is a builder for a YAML file, and nothing more. Everything the UI can express,
the file can — and the reverse, which is the harder promise to keep. Press
**Copy as YAML** and you have a scheduled job:

```yaml
name: dentists-north-london
sources: [{id: gmaps, options: {pause: 2}}]
where:   {mode: paste, places: [Islington London, Camden London]}
what:    {packs: [trades/dentist]}

filters:                                    # nine operators, not one
  - {signal: web_presence, op: in, value: [none, social_only, booking_only]}
  - {signal: reviews, op: ">=", value: 30}
  - {signal: chain_size, op: "<=", value: 3}
  - {signal: trade_match, op: "!=", value: false}

scoring:
  weights:
    reviews:      {weight: 35, scale: log, cap: 300}
    web_presence: {none: 40, social_only: 40, owned_domain: 0}
  normalise: true

limits:                                     # a collector without a ceiling is
  max_results: 500                          # how you wake up to a banned IP
  max_runtime_seconds: 1800
  max_requests: 2000
  workers: {discover: 3, profile: 3}

suppress:                                   # prospecting is weekly
  lists: [contacted.csv]                    # applied BEFORE anything is measured
  runs:  [88c16e73a314]                     # hides unchanged verdicts only
  after: 90d
```

Two things in there are worth calling out.

**Suppression is not deduplication.** A business you skipped last month because it
had a website, whose site is now dead, is the best lead in the file. Kerb applies
"seen before" *after* judging, and only when the verdict is unchanged. A changed
verdict is news.

**Re-ranking is free.** The measurements already exist, so moving a weight slider
re-scores in place without collecting anything again.

---

## Staying unblocked

Kerb is polite by construction, because the alternative is an address that stops
working:

- **Jittered pacing.** Nothing human requests every 2.000 seconds.
- **Persistent cooldown.** A block is recorded *to disk* and backs off harder each
  time — 15m, 30m, 1h, capped at 6h — decaying after a quiet day. The next run
  **refuses to start** while it's cooling, because walking back into a live block
  is how a short penalty becomes a long one. A clean run clears it.
- **It never fights a block.** No CAPTCHA solving, no evasion. A consent wall or a
  429 stops the run after *one* call and tells you.
- **Budgets count retries.** A budget that ignores them is not a budget.

```bash
kerb setup --check     # is the profile working? am I cooling down?
```

---

## Commands

| Command | Does |
|---|---|
| `kerb` | Opens the UI |
| `kerb setup` | Builds the browsing profile. `--check` verifies it, `--import-cookies` signs it in |
| `kerb run brief.yaml` | Runs headless, writes CSV / JSON / JSONL |
| `kerb estimate brief.yaml` | What it *would* do, without doing it |
| `kerb reviews <run>` | Harvests review text for a finished run — opt-in, needs `kerb[browser]` and a signed-in profile |
| `kerb requalify <run>` | Re-judge a stored run with today's packs and rules — no re-collection |
| `kerb jobs` | Durable runs: what finished, what's left |
| `kerb doctor` | Checks the install, the profile, and stray processes |
| `kerb stop` | Kills everything Kerb started, and verifies it |

`kerb stop` exists because the tool this replaced leaked browser processes until a
disk filled. Kerb launches none — but it still checks.

---

## What Kerb does not do

Every project has these. Most hide them.

- **Reviews without a browser.** The review RPC's headers are session-scoped and
  cannot be synthesised, so review counts and review text need
  [`kerb[browser]` and a signed-in profile](#reviews--opt-in-and-the-one-place-a-browser-is-needed).
  Signed out, Kerb reports those businesses as *never found out* rather than
  inventing a count — a guessed one silently changes every score — and the UI
  offers `rating_band` instead, which **is** collected over plain HTTP.
- **Synonyms.** A typed trade matches by substring, so `cafes` will not find
  "Coffee shop" until you add `coffee` to the keywords. Google knows they're
  synonyms; a string matcher doesn't.
- **CAPTCHAs and blocks.** Never solved, never bypassed. See above.
- **Personal data.** Kerb collects business listings — name, category, address,
  phone, website, rating. Not people.

---

## Contributing

The endpoint Kerb reads is undocumented, so the most valuable contribution is
usually a **field map fix**. When Google moves a field, one number changes:

```python
# kerb/sources/gmaps.py
FIELDS = {"cid": 10, "name": 11, "categories": 13, "address": 39, ...}
```

The map is data, not code, on purpose. And if the shape moves, `parse()` raises
`ShapeChanged` rather than returning an empty list — because a collector that
silently returns nothing is indistinguishable from a genuinely empty area, and
that is the exact dishonesty this project exists to remove.

```bash
git clone https://github.com/Startrekl/kerb && cd kerb
pip install -e ".[server]"
python3 -m pytest tests/ -q      # 170 tests, all offline
```

Tests are offline by design — one runs against a *real* captured response, trimmed
to a fixture, so a shape change breaks a test rather than a user's afternoon.

---

## Legal

Kerb reads public business listings the way the Maps site does. **This is against
Google's Terms of Service**, and the endpoint is undocumented and may change without
notice.

Kerb never solves a CAPTCHA and never works around a block — when it meets one it
stops, records a cooldown, and tells you. It collects business information, not
personal data. Nothing leaves your machine: there is no telemetry, no account and
no server but yours.

Use it at your own risk, and check what applies where you are.

## License

[MIT](LICENSE) © Sachin Gautam
