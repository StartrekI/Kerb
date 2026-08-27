# Kerb — Front-End Requirements

> **Naming note.** This is deliberately *not* called `requirements.txt`. At the root
> of a Python package that filename belongs to pip, and putting prose there breaks
> `pip install -r requirements.txt`. This document is the front-end brief.

**Deliverable:** one file, `kerb/static/index.html`, containing the entire
application — markup, CSS and JavaScript inline. It is served at `/` by the Kerb
Python server and is the only page in the product.

**Audience for this document:** whoever (or whatever) generates the UI. Everything
in §1–§4 is context you need to design well. Everything in §8 is a hard contract:
break it and the automated test harness fails and the build does not ship.

---

## 1. What the application does

### 1.1 The problem, concretely

A freelance web designer wants dentists in North London who **do not have a proper
website** — those are the people who might buy one. Today that means: run a Maps
scraper, get 400 rows of CSV, then spend an afternoon in a spreadsheet manually
checking which ones have a real site, which are actually dentists (the scraper
returned dental *labs* and a rooftop café called "The Molar"), which closed down
two years ago, and which are branches of a 40-location chain that already has an
agency.

By the end they have maybe 25 real leads, no record of why they discarded the other
375, and next week they will do the identical afternoon again and re-discard the
same 375.

**Kerb is that afternoon, written down as a specification and executed in seconds —
with the reasoning kept.**

### 1.2 What it actually does, step by step

The user writes a **brief**: where the data comes from, what counts as the trade,
what disqualifies a business, and how to rank whatever survives. Kerb then runs a
pipeline over every business:

```
DISCOVER    Pull businesses from the chosen sources. Deduplicate on
            business id as results arrive, not afterwards.

SUPPRESS    Drop anyone on a "already contacted / not a fit" list — before
            (hard)      any measurement, so it also saves the requests.

MEASURE     Run signals in COST TIERS, cheapest first:
& JUDGE       FREE      → data already in the row (review count, whether
                          the category matches the trade, chain size)
              CHEAP     → one network request each (is the website
                          actually alive? what is it built on?)
              EXPENSIVE → reserved, opt-in only

            After each tier, test the brief's conditions against what has
            been measured SO FAR. The moment a business fails one, stop —
            it never reaches the paid tier. This is the whole reason the
            expensive tier stays affordable: most businesses are decided
            on free data alone.

SCORE       Survivors get a weighted score from their measurements, and
            optionally a band ("A", "hot lead", whatever the user names).

SUPPRESS    Hide businesses seen in a previous run — but ONLY if the
(soft)      verdict is unchanged. One you skipped last month because it
            had a website, whose site is now dead, is the best lead in
            the file. It resurfaces.
```

Every measurement is recorded as a **`(value, confidence, evidence)`** triple, and
every one is kept. That is what makes the dossier possible: not "score 93.9" but
*"web_presence = none, confidence 1.0, +40 points"* and *"trade_match = dentist,
confidence 0.95, +25, because the listing category was 'Dentist' and the rule came
from pack trades/dentist@1"*.

### 1.3 The idea the whole product is built on

**Every verdict carries its evidence, and a failed measurement is never a verdict.**

Competing tools hand you a list of names and a number. Kerb shows which measurement
produced that number, how confident it was, and the raw data behind it — and when
it could not measure something, it says *"we never found out"* rather than quietly
scoring it zero and letting it look rejected.

That second half came from a real bug in the predecessor tool: a website check that
timed out was recorded as "no website", so businesses got contacted on the strength
of a network error. Hence three outcomes rather than two (§3), and hence the run
warnings in §5.3.

**The interface exists to make that argument visible.** A design that hides the
reasoning behind a click has failed the brief, however handsome it is. If a user has
to open a panel to find out why one row outranks another, the product's entire
differentiator is buried.

### 1.4 A session, start to finish

This is the shape the UI has to support:

1. **Compose.** Opens on the brief — pre-filled from last time, because next week's
   run is usually last week's with one change. Picks the source, points at a CSV,
   clicks *Inspect* and sees "412 rows, detected gosom, mapped 9 fields". Picks
   *Dentist*. Leaves the four default conditions on, adds `chain_size <= 3`.
   Reads the sentence at the top — *"Find Dentist in dentists-london.csv, keeping
   only those where review count >= 30, verified trade match, web presence ∈
   none/social_only/booking_only, chain size <= 3, then rank what survives"* — and
   spots that they forgot to exclude closed businesses. Ticks *Liveness = open*.
2. **Run.** Watches a live log. Takes seconds on a file, minutes on OpenStreetMap.
3. **Read the warnings first.** *"3 businesses could not be measured"* — fine.
   *"Discovery looks degraded — 41% of expected yield"* — not fine, that means the
   source is throttling and the results are incomplete; re-run later.
4. **Scan.** 38 qualified out of 412. Reads down the list judging on the evidence
   chips alone: *no website · dentist · excellent rating · 142 reviews*. Arrow-keys
   through, opens three dossiers to check the reasoning holds.
5. **Adjust without re-running.** Decides review count matters more than web
   presence, drags a slider; the list reorders instantly because the measurements
   already exist. This surprises people — say so in the UI.
6. **Check the rejects.** Clicks *Rejected*, scans reasons, sees a real dentist
   thrown out by an over-tight condition, goes back and loosens it.
7. **Export.** CSV of the current view, into a mail merge.
8. **Graduate.** Copies the brief as YAML to run it on a schedule from the CLI.

### 1.5 What this means for the UI, in one line each

- **The brief is a document, not a form.** It is read back, checked, and reused.
- **Evidence belongs on the row.** Step 4 is the product; it must work without clicking.
- **Warnings outrank results.** Step 3 comes before step 4 for a reason.
- **Re-ranking is free and re-briefing is cheap.** Steps 5–6 must not feel like starting over.
- **Rejections are content, not a wastebasket.** Step 6 is how a user learns their own brief.

### 1.6 Who uses it

A freelancer or small agency doing weekly prospecting. Technical enough to run a
local server, not a developer. Runs a survey, scans 50–500 results, opens the
promising ones, exports a CSV, emails people. Will do this again next week with a
slightly different brief.

They are not exploring data for its own sake and they are not building a report for
someone else. They are deciding who to contact on Monday.

### 1.7 What they need from the page, in priority order

1. Judge a ranked list quickly, without clicking into each row.
2. Trust the ranking — see why #1 is above #7.
3. Notice when the run itself went wrong (a source failed, half the area was
   unreadable) rather than quietly acting on partial data.
4. Change the brief and re-run without rebuilding it from scratch.

---

## 2. Non-negotiable technical constraints

| Constraint | Why |
|---|---|
| **Single self-contained file.** No build step, no bundler, no `npm`. | It ships inside a Python wheel. |
| **Zero external network requests.** No CDN scripts, no Google Fonts, no remote images, no analytics. | Enforced by an automated test (`test_api.py::test_ui_is_self_contained`). It is a self-hosted tool that must look right with the network unplugged. |
| **System font stack only.** Character must come from *pairing and treatment*, not from a downloaded face. | Same reason. A webfont link fails to a silent fallback exactly when someone is offline. |
| **No framework.** Vanilla JS. | No build step; also keeps the file readable as the reference implementation. |
| **Works at 1280×800 and up.** Below that, degrade gracefully; no horizontal page scroll ever. | Asserted by the harness (`bodyNoHScroll`). |
| **Light and dark themes, both designed.** | See §7. |
| Respect `prefers-reduced-motion`. Visible keyboard focus on every control. | Accessibility floor. |

---

## 3. Domain vocabulary — get this right or the UI lies

These are not style preferences. Using the wrong word here makes the product
misreport its own results.

### Three outcomes, never two

| Outcome | Means | Must never be shown as |
|---|---|---|
| `qualified` | Measured, and it passes every condition. Has a `score`. | — |
| `rejected` | Measured, and it fails at least one condition. Has a `reject_reason`. | — |
| `unevaluated` | **We could not measure it.** A site timed out, a source died mid-run. | ❌ Never styled, coloured, grouped or worded as a rejection. |

`unevaluated` is the outcome the whole architecture exists to protect. A failed
measurement is not a verdict about the business. It gets its own tab, its own
colour (amber, not red), and language like *"could not be measured — we never found
out"*, never *"failed"* or *"rejected"*.

### Cost tiers

Every signal is `free`, `cheap`, or `expensive`. `free` reads data already in hand.
`cheap` and above spend a network request **per business**.

- Paid-tier signals must be visibly marked in the UI.
- A paid signal must **never** be switched on by default. Ticking one starts
  spending requests; that has to be a decision the user made.

### Confidence

Every measurement carries a `confidence` from 0 to 1. `web_presence: none` at
confidence 1.0 and at confidence 0.3 are different claims and must not render
identically.

---

## 4. The data — real shapes from the live API

All endpoints are same-origin. `GET` unless noted.

### 4.1 Boot (call all four in parallel on load)

```
GET /api/health   → {"status":"ok","version":"0.1.0","signals":14,"sources":3,"packs":14}
GET /api/signals  → [Signal]   (see table below)
GET /api/sources  → [Source]
GET /api/packs    → [Pack]
```

**Source:**
```json
{ "id":"csv", "label":"Import a file",
  "description":"Any scraper's CSV / JSON / JSONL export",
  "needs_key":false, "needs_browser":false, "ready":true,
  "legal_note":"Uses data you already hold. Kerb fetches nothing." }
```
Three sources exist: `csv`, `gosom` (both need a **file path**) and `overpass`
(needs a **list of places**). `legal_note` must be displayed — for `overpass` it
carries an ODbL attribution requirement.

**Pack:** `{"id":"trades/dentist","kind":"trades","label":"Dentist","counts":{"categories":8,"keywords":5,"veto_categories":3}}`
Kinds: `trades`, `geo`, `chains`, `web-presence`. Only `trades` packs are picked by
the user; the rest are infrastructure. `trades/_shared-vetoes` is not a
user-selectable trade.

### 4.2 The signal registry — **the filter UI must be generated from this, never hardcoded**

This is a hard requirement with history: a hardcoded list drifted ten signals
behind the registry, so the UI could not express things the config file could.

| name | label | kind | cost | values | default filter |
|---|---|---|---|---|---|
| `web_presence` | Web presence | categorical | free | none, social_only, booking_only, builder, owned_domain, unknown | `in [none, booking_only, social_only]` **on** |
| `reviews` | Review count | number | free | — | `>= 30` **on** |
| `trade_match` | Trade match | boolean | free | — | `!= false` **on** |
| `liveness` | Liveness | categorical | free | open, temp_closed, perm_closed, stale, unknown | `== open` **on** |
| `chain_size` | Chain size | number | free | — | `<= 3` |
| `contactable` | Contactable | boolean | free | — | `== true` |
| `rating_band` | Rating band | categorical | free | excellent, good, mixed, poor, unrated, unknown | — |
| `review_integrity` | Review integrity | categorical | free | complete, truncated, unavailable | — |
| `review_velocity` | Review velocity | number | free | — | — |
| `establishment_age` | Establishment age | number | free | — | — |
| `name_script` | Name script | categorical | free | latin, cyrillic, greek, arabic, hebrew, devanagari, han, hiragana, katakana, hangul, thai, tamil, bengali, telugu, unknown | `in [latin]` |
| `site_status` | Website status | categorical | **cheap** | live, dead, parked, placeholder, no_site, unknown | `in [dead, parked, placeholder, no_site]` |
| `site_platform` | Site platform | categorical | **cheap** | wix, squarespace, shopify, wordpress, godaddy, weebly, webflow, duda, square, facebook, custom, unknown | — |
| `site_contact` | Contact on site | text | **cheap** | — | — |

Render `categorical` as multi-select chips when the operator is `in`, a dropdown
when `==`; `number` as a numeric input; `boolean` as a two-option select. `text`
signals are not filterable.

> **Trap, already hit once:** `trade_match`'s default is `!= false`, which is the
> same requirement as `== true`. Rendering the raw value under a "must be" label
> displayed the shipped default as **"must be false"** — the exact opposite of what
> it does. Show the *effect*; put the operator back when building the campaign.

### 4.3 Validate (debounced, on every change)

```
POST /api/campaigns/validate  {"campaign": {...}}
  → {"valid": true,  "problems": []}
  → {"valid": false, "problems": ["where.places is empty", ...]}
```
Disable Run while invalid and show the problems.

### 4.4 Inspect a file before committing to it

```
POST /api/import/inspect {"path":"..."} →
{ "rows":10, "detected_profile":"gosom", "mapping":{...},
  "unmapped_columns":["notes"], "warning": null }
```

### 4.5 The campaign object (what the builder produces)

```json
{
  "name": "dentist",
  "sources": [{"id":"csv","options":{"path":"tests/fixtures/gosom_export.csv"}}],
  "where":   {"mode":"paste","places":["Islington, London"]},
  "what":    {"packs":["trades/dentist"]},
  "filters": [{"signal":"reviews","op":">=","value":30},
              {"signal":"web_presence","op":"in","value":["none","social_only"]}],
  "scoring": {"weights":{"reviews":{"weight":35,"scale":"log","cap":300},
                         "web_presence":{"none":40,"owned_domain":0}},
              "normalise": true}
}
```
`where` is `{}` for file sources. The same object must be exportable as YAML — the
CLI consumes exactly this, and copy-as-YAML is how a user graduates from the UI to
a scheduled job.

### 4.6 Run, and stream progress

```
POST /api/runs {"campaign":{...}} → 202 {"id":"88c16e73a314"}
GET  /api/runs/{id}/events                    Server-Sent Events
POST /api/runs/{id}/stop
```

Event shapes (each carries a monotonic `i` and elapsed `t`):
```
{"stage":"discover","source":"overpass","status":"start"|"ok","found":123,"i":4,"t":2}
{"stage":"qualified","name":"Bright Smile Dental","score":93.3,"i":9,"t":5}
{"stage":"source_error","source":"overpass","error":"ConnectFail"}
{"stage":"discover_degraded","source":"overpass", ...}
{"stage":"breaker", ...}          measurement failure rate tripped
{"stage":"stop","reason":"result cap reached"}
{"stage":"done", ...stats}
{"stage":"finished","status":"done"|"partial"|"failed"|"stopped"|"interrupted"}
```

> **Two traps here, both previously shipped as bugs.**
> 1. `EventSource` reconnects on its own and the server **replays from the start**.
>    De-duplicate on `i` or the log shows everything twice.
> 2. `onerror` is **not** the end of a run — it fires on every reconnect. Only treat
>    the run as over after asking `GET /api/runs/{id}` and seeing a terminal status.
>    `partial` and `interrupted` are terminal. Do not report either as "done".

### 4.7 Results

```
GET /api/runs/{id} →
{ "id","name","status","error","elapsed","qualified","rejected","unevaluated",
  "total","events_dropped","live",
  "stats": { "discovered":10, "deduped":10, "qualified":6, "rejected":4,
             "unevaluated":0, "suppressed":0, "requests":0,
             "elapsed_seconds":0.0, "qualify_rate":0.6, "per_minute":null,
             "rejected_by": {"web_presence":3, "reviews":1},
             "source_errors": {"overpass":"ConnectFail: connection refused"},
             "skipped_places": {"overpass/Hackney":"could not be geocoded"},
             "discovery_health": {"verdict":"degraded","yield_pct":41,
                                  "suspect":6,"searches":14,"baseline":22,
                                  "flagged":["Hackney","Soho"]},
             "stopped_reason": null } }

GET /api/runs/{id}/businesses?limit=5000&sort=score
  → {"total":10,"offset":0,"limit":5000,"rows":[Business]}

GET /api/runs/{id}/businesses/{cid}        one business, plus `explain`
POST /api/runs/{id}/rescore  {"weights":{...},"normalise":true}
GET  /api/runs/{id}/export?format=csv
```

**Business** (list and detail share these; `cid` may contain `/` and `:` — always
`encodeURIComponent` it):
```json
{ "cid":"0x1a:0x00a", "name":"Islington Family Dentist", "category":"Dentist",
  "address":"40 Upper St, London", "phone":"+442075555555",
  "website":null, "booking_url":null, "rating":4.9, "review_count":142,
  "lat":51.54, "lng":-0.1, "source":"csv:gosom", "place_label":null,
  "score":93.9, "outcome":"qualified", "band":null, "qualified":true,
  "rejected_by":null, "reject_reason":null, "failed_signals":[],
  "breakdown": {"reviews":30.44, "web_presence":40.0},
  "signals": {
    "contactable": {"name":"contactable","value":true,"confidence":1.0,
                    "evidence":{"ways":["phone"]},"version":1,"failed":false}
  },
  "extras": {"...raw source columns..."} }
```

**`explain`** (detail endpoint only) — the ranked scoring breakdown:
```json
[{"signal":"web_presence","value":"none","confidence":1.0,"points":40.0,"evidence":{}},
 {"signal":"trade_match","value":"dentist","confidence":0.95,"points":25.0,
  "evidence":{"matched_category":"Dentist","rule":"Dentist","pack":"trades/dentist@1"}}]
```

Note `signals` contains **more** entries than `breakdown` — things that were
measured but not scored. Both are worth showing; they are different claims.

---

## 5. Screens and states

There is **no permanent sidebar.** A rail spends a fifth of the window on controls
nobody touches while reading results, and squeezes the records — the actual
product — into what is left. Each mode gets the full width.

### 5.1 THE BRIEF (compose)

The default state on load. The campaign builder *is* the page.

Must contain, laid out to use the full width:

1. **Where the data comes from** — source picker; then either a file path (with an
   "Inspect file" action showing row count, detected profile, unmapped columns) or
   a places textarea. Show the source's `legal_note`.
2. **What counts as the trade** — trade pack picker, with that pack's counts
   (categories / keywords / vetoes) so the choice is informed.
3. **What disqualifies** — all ~13 filters, generated from `/api/signals`, each with
   an on/off toggle, its control, its description, and a cost badge if not free.
   These vary wildly in height (a number input is one line; `name_script` is fifteen
   chips), so **a plain CSS grid leaves holes the height of the tallest cell** — use
   CSS multi-columns or a masonry approach.
4. **How to rank what survives** — one weight slider per active signal, generated
   the same way. These are uniform, so a grid is correct here.
5. **A live readback of the brief in plain language** — "Find *Dentist* in
   *gosom_export.csv*, keeping only those where `review count >= 30`, …, then rank
   what survives." A campaign is a specification; reading it back as a sentence
   catches mistakes that thirteen separate controls hide.
6. **A commit bar** — validation problems, Copy-as-YAML, and the primary Run action.
   It must stay reachable however far down the brief you have scrolled.

Each step should indicate whether it is actually configured. Four identical-looking
cards is how a run gets started with an empty source.

### 5.2 THE RUN (transient)

Full-page. A live log driven by SSE, plus honest framing of what is happening
("cheap tests first — most businesses are decided before anything expensive runs").
A Stop control must be reachable throughout.

### 5.3 THE FINDINGS (results)

- **The brief, collapsed to one line** at the top, with actions to change it or
  re-run. The user must never have to rebuild a brief to tweak it.
- **Run-level warnings**, before the results, for: unevaluated businesses; degraded
  discovery health; source errors; skipped places; truncation (showing the first
  5,000 of 40,000); dropped log events. *"Fewer results with no explanation" is the
  exact failure this product exists to remove.*
- **Funnel** — qualified vs each rejection reason, proportionally, with counts and
  percentages.
- **Score distribution** — where the qualified scores actually fall. A single
  average hides a bimodal list: "82 average" reads identically whether that is
  forty solid leads or twenty perfect ones and twenty you should not call.
- **Tabs**: Qualified / Rejected / Unevaluated / All, with live counts. The
  Unevaluated tab is hidden when the count is zero.
- **Search** across name, category, address. **Sort** by score, reviews, name — first
  click sorts the way that column is read (highest score first, most reviews first,
  names from A), second click reverses, with a visible direction indicator.
- **Export CSV** of the current view.
- **The record list** — full width. Each record shows the score, the business name,
  category and address, **its findings inline as evidence chips**, review count, and
  its assessment (reject reason where there is one).
- **The dossier** — opening a record shows the listing fields, the ranked scoring
  breakdown from `explain` (with each contribution's value, confidence, points and
  raw evidence), and the signals that were measured but not scored.

### 5.4 Empty, loading and failure states — all required

| State | Requirement |
|---|---|
| Server unreachable at boot | Say so plainly, on screen. Do not sit there looking fine and empty. |
| Search matches nothing | An empty-state message, not a blank table. |
| Run fails to start | Show the server's message. |
| Run returns zero qualified | Explain it (the brief was too narrow) and offer the way back. |
| A tab with zero rows | Empty state. |

---

## 6. Interaction requirements

- **Live re-ranking.** Moving a weight slider with results on screen calls
  `/rescore` and reorders in place. Signals are already measured, so this costs
  nothing — say so in the UI, because users assume re-running is expensive.
- **Keyboard.** ↑/↓ move through records, ↵ opens the dossier, Esc closes it.
  Must not hijack typing in the search box. 200 leads is a keyboard job — advertise
  the shortcuts on screen.
- **Debounced validation** (~200ms) so the Run button reflects reality without a
  request per keystroke.
- **Persist the brief** to `localStorage` and restore it next session.
- **Clipboard has a silent failure mode**: `navigator.clipboard` is undefined
  outside a secure context, which `--host 0.0.0.0` gives you. Fall back to
  `execCommand("copy")`, then to a selectable textarea. Never claim "Copied" when
  nothing was copied.
- Evidence chips must say the finding, not the machine token. `none` → "no website",
  `booking_only` → "booking page only". Unmapped values fall through with
  underscores opened, so a new signal value is legible the day it ships.

---

## 7. Visual direction

Formal, premium, professional — a serious tool, not a SaaS marketing page. It
should look like instrumentation someone bought, not a template.

**Required:**
- A deliberate palette defined as CSS custom properties on `:root`, with **semantic
  colour (good / warning / critical) kept separate from the accent hue** so
  "emphasised" never reads as "good".
- Restraint with the accent — ideally one accent, spent on the two things that
  matter most (the score and the primary action).
- Real typographic hierarchy from the system stack. Pair a serif and a grotesque
  rather than setting everything in one face. `font-variant-numeric: tabular-nums`
  everywhere digits align.
- **Both themes structured token-level**, in three blocks: bare `:root` (complete
  light palette), `@media (prefers-color-scheme: dark)` guarded as
  `:root:not([data-theme="light"])`, and `:root[data-theme="dark"]`. The default
  "system" setting stamps *nothing* on the root element, so a colour whose only
  definition sits inside a `[data-theme]` block never applies there. A manual
  toggle cycles dark → light → system and persists.
- Density suited to scanning hundreds of rows — but never so tight that the
  evidence gets squeezed out. Evidence is the product.

**Avoid** (these read as machine-generated): warm cream `#F4F1EA` with a serif
display and terracotta accent; near-black with one acid-green pop; a purple-to-blue
gradient hero; Inter or Space Grotesk as the safe default; emoji as section markers;
everything centred; rounded cards with a coloured left rail.

---

## 8. HARD CONTRACT — an automated harness drives these

`kerb/static/_smoke.html` loads the real app in an iframe and drives it through
~65 assertions against a live server. **Every id, attribute and class below must
exist and behave as described**, whatever the visual design.

### 8.1 Element `id`s

```
Boot/brief:  ver  source  sourceNote  pathField  path  inspectBtn  inspectOut
             placesField  places  pack  packNote  filters  weights
             problems  runBtn  stopBtn  yamlBtn  yamlOut  yamlText*
Results:     brief  editBtn  warnings  funnel  topStats  toolbar
             search  csvBtn  tableWrap  rows  drawer  stage  log*
Counts:      cQ  cR  cU  cA  tabU
Weights:     wv-<signalName>   e.g. wv-reviews
Chrome:      themeBtn
```
`*` created at runtime, not in static markup.

### 8.2 Attributes and classes

| Selector | Contract |
|---|---|
| `[data-on="<signal>"]` | checkbox enabling a filter |
| `[data-f="<signal>"]` | that filter's single-value control |
| `[data-multi="<signal>"]` | checkbox in a multi-select filter |
| `[data-w="<signal>"]` | `<input type=range>` weight slider |
| `[data-view="qualified\|rejected\|unevaluated\|all"]` on `.tab` | result tab, with `aria-selected` |
| `th.sortable[data-sort="score\|reviews\|name"]` | sortable column header |
| `#rows tr[data-cid]` | one result row (must be a real `<tbody>` row) |
| `td.num` | numeric cells; **the second one in a row is review count** |
| `.score` | the score cell |
| `.name` | the business-name element inside a row |
| `.reason` | the assessment / reject-reason cell |
| `.funnel-seg` | one funnel segment |
| `#topStats .stat` | one headline statistic |
| `.card > h3`, `.card .hint` | measured for text clipping — must not crop |
| `.ev` | one evidence entry in the dossier |
| `.notice` | one warning block |

### 8.3 Debug surface

```js
window.kerb = { S, renderTable, renderWarnings, renderFunnel, visible, show };
```
`S` is the live state object (`S.rows`, `S.view`, `S.sort`, `S.run`, `S.sel`).
The harness sets `S.rows` directly and calls `renderTable()`; it calls
`renderWarnings(summary, page)` with synthetic failures to check that every kind of
trouble renders. Keep these callable with no side effects beyond re-rendering.

### 8.4 Behavioural assertions

- Overpass source with no places → Run **disabled**, problem shown.
- Bad file path → `#inspectOut` contains `.notice.bad`.
- After a run: `#stage` hidden, `#stopBtn` hidden, `#funnel` shown, rows sorted by
  score descending.
- Every rejected row has non-empty `.reason` text.
- Unevaluated rows are **not** styled as rejections.
- `#tabU` hidden when the unevaluated count is 0, shown when it is not.
- Moving `[data-w=reviews]` changes scores, updates `#wv-reviews`, produces no `NaN`.
- Theme button cycles `data-theme`: `dark` → `light` → *(absent)*.
- No horizontal page scroll, in either mode. Zero clipped text nodes.
- The primary Run action is on screen when the brief is scrolled to the top.
- Zero uncaught JS errors across the whole run.

---

## 9. Acceptance

```bash
python3 -m kerb serve --port 8811          # then, headless:
#   /assets/_smoke.html?w=1500&h=1250&theme=dark   → 65 checks, errors=[], DONE=1
python3 -m pytest tests/ -q                # → 152 passed
python3 -m kerb stop                       # → clean, nothing left
```

A design is finished when the harness is green, both themes are legible, and a
stranger can look at one qualified row and say why it is above the row beneath it
**without opening anything.**
