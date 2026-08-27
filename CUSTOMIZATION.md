# What customers should be able to change

An analysis of where kerb's configuration surface actually runs out, derived
from evidence rather than imagination: the scripts a real operator had to write
by hand *because* the tool could not express what they needed.

---

## 1. The evidence

The predecessor pipeline (`scrap_business`) carries eleven hand-rolled scripts
and twenty-one hand-maintained text files. They are not miscellaneous — they
cluster into four groups, and each group is one missing idea:

| Hand-rolled | What it was compensating for |
|---|---|
| `find_new_listings.py`, `audit_new_listings.py`, `reconcile_local_db.py`, `restore_fitness36.py` | **No memory across runs** |
| `places_*.txt` × 8, `queries_*.txt` × 13 | **No home for curated targeting** |
| `reorder_by_city_priority.py`, `reorder_europe_queue.py` | **No control over run order** |
| `remove_wrong_trade_roofing.py` | **No way to re-judge stored results after a rule change** |
| `push_to_scarrper.py` | **No output beyond a file** |

The pack architecture already *names* `geo` and `chains` as pack kinds. Neither
ships. Meanwhile the operator maintains eight place lists by hand.

Two things are worse than missing — they are configuration that looks supported
and silently is not:

- **`what.packs` accepts a list and uses only the first.** A campaign listing
  `[trades/dentist, trades/medical, trades/vet]` rejects every vet as "not the
  trade", with a confident reason. Verified.
- **The `output:` block is parsed and never read.** Anything written there is
  silently discarded.

Both are bugs, not features, and should be fixed before anything below is added.

---

## 2. The organising distinction

Everything that follows is one of two things, and conflating them is how
roadmaps go wrong:

**Capabilities** change what the tool can *know*. There is exactly one missing:
memory across runs. Every script in the first row of that table exists because
each run starts blind.

**Parameters** change what the tool *does* with what it knows. There are many,
most are cheap, and several are free — computable from data already in hand,
costing no requests at all.

Free parameters are strictly the best value in the project. They are also the
ones nobody asks for, because a user cannot request a signal they do not know
is derivable.

---

## 3. Tier 1 — the capability: memory

### 3.1 Suppression

The single most valuable thing to add. Ongoing prospecting is longitudinal: you
contact people, you form judgements, and both must stick.

```yaml
suppress:
  - list: contacted.csv          # any file with a cid column
  - list: not-a-fit.csv          # a human said no; that decision is durable
  - runs: [a3f9c21, 7b02de4]     # everything a previous run surfaced
  - domains: [ourclients.com]    # existing clients
  - after: 180d                  # allow re-surfacing eventually
```

Without this, a weekly run hands back the same five hundred leads every week
and the operator filters them in a spreadsheet — which is precisely what
`find_new_listings.py` was doing.

`after:` matters more than it looks. Permanent suppression is wrong: a business
that had a website last quarter and lost it this quarter is now a *prospect*,
and a suppression list with no expiry would hide exactly the lead you most want.

### 3.2 Delta runs

```bash
kerb run campaign.yaml --since last       # new since the previous run
kerb run campaign.yaml --since 2026-07-01
kerb run campaign.yaml --changed          # seen before, but the verdict moved
```

`--changed` is the interesting one and only becomes possible once results are
stored. A business whose `site_status` went `live → dead` since last month is
the highest-intent lead the dataset can produce, and today nothing can find it.

Output gains `first_seen`, `last_seen`, `previous_outcome`.

### 3.3 Re-judging stored results

`remove_wrong_trade_roofing.py` existed because a taxonomy fix required a data
migration. With results in the store, that becomes a command:

```bash
kerb requalify <RUN_ID>                  # re-run filters with today's packs
kerb requalify <RUN_ID> --diff           # show what would change, change nothing
```

Free, since every signal is already stored. `--diff` first is the honest
default — a rule change that silently deletes 23 businesses is how the
predecessor over-deleted two good ones.

---

## 4. Tier 2 — free signals

Each is computable from data already held. No requests, no keys, no latency.

### 4.1 `chain_size` — independent or branch?

A freelancer selling websites cannot sell to a chain. Compute it from the
dataset itself: normalise the name, count occurrences.

```yaml
filters:
  - { signal: chain_size, op: "<=", value: 3 }     # independents only
```

Plus a `chains/` pack for brands recognisable from one row (Specsavers, Boots).
This is the highest value-per-line item on the list: it removes a whole class of
unsellable lead, and it is arithmetic.

### 4.2 `name_script` — the writing system of the name

Requested explicitly and handled by hand: *"English business names only"*.
Outreach written in English to a business named in Cyrillic or Devanagari is
wasted, and the operator was filtering these manually.

```yaml
filters:
  - { signal: name_script, op: in, value: [latin] }
```

Free: `unicodedata` on the name. Should report the dominant script plus a
confidence, since real names mix scripts.

### 4.3 `review_velocity` — reviews per year

Distinguishes a dentist with 200 reviews over fifteen years (established,
sleepy, no budget) from 200 over two (growing, spending). Same review count;
completely different prospect.

```yaml
scoring:
  weights:
    review_velocity: { weight: 20, scale: log, cap: 50 }
```

Free: `review_count / (now - first_review_year)`. Must inherit
`establishment_age`'s caveats rather than restate its proxy as fact.

### 4.4 `contactable` — can you actually reach them?

A lead with neither phone nor email is not a lead. Trivial, and it belongs in
the default filter set of every campaign.

### 4.5 `rating_band`

Sometimes you want the badly-rated (they need help); sometimes the well-rated
(they have money and care). Currently expressible only as a raw number.

---

## 5. Tier 3 — the configuration surface

### 5.1 Geo packs

Ship what the operator maintains by hand. Versioned, shareable, diffable:

```yaml
where:
  packs: [geo/uk-affluent-towns, geo/us-metro-top-100]
  exclude: [geo/already-covered]
  order: priority
```

Candidates to ship: `uk-affluent-towns`, `us-metro-top-100`,
`europe-english-speaking`, `gcc-premium`, `india-tier-1`. Every one of these
already exists as a `.txt` file in the predecessor.

### 5.2 Run order

```yaml
where:
  order: priority | as-listed | random | alphabetical
  priority: [London, Manchester, Birmingham]
```

Order only matters when a run does not finish — which is exactly when it
matters most. A run stopped by the breaker at 40% should have spent that 40% on
the places worth the most. Two hand-rolled reordering scripts say this is real.

### 5.3 Multi-trade campaigns

Fix the silent truncation, then make it deliberate:

```yaml
what:
  packs: [trades/dentist, trades/medical]
  match: any        # any | all
```

### 5.4 Output shaping

The `output:` block is already parsed. Make it mean something:

```yaml
output:
  columns: [name, phone, website, score, site_status, reject_reason]
  min_score: 70
  top: 200
  split_by: place            # one file per city, for handing to a caller
  template: outreach.csv     # mail-merge column names
```

`split_by` is small and disproportionately useful: prospecting work is handed
out per-territory, and splitting is currently a spreadsheet chore.

---

## 6. Tier 4 — scoring honesty

### 6.1 Confidence-weighted scoring

A genuine conceptual inconsistency. `web_presence` read directly from the
website field has confidence `1.0`; the same value inferred from a booking-field
redirect has `0.9`. **They currently score identically.** A project whose whole
argument is that uncertainty must stay visible discards it at the last step.

```yaml
scoring:
  confidence: weight     # points *= signal confidence
```

Opt-in, because it changes every existing score. Default off; recommended on.

### 6.2 Score-band explanations

```yaml
scoring:
  bands:
    - { min: 85, label: "call today" }
    - { min: 70, label: "worth a look" }
```

A number ranks; a label is what someone acts on.

---

## 7. What not to build

Worth stating so it does not get re-proposed:

- **Proxy rotation, fingerprint spoofing, CAPTCHA handling.** Out of scope by
  choice, not by omission.
- **A general rules DSL.** The filter/weight grammar is already close to one.
  Adding `and`/`or`/arithmetic makes campaigns unreviewable and moves the
  complexity from code into YAML without removing it.
- **A CRM.** Suppression lists are the boundary. Read `contacted.csv`; do not
  try to own it.
- **Per-signal retry/timeout knobs.** The collector owns that. Exposing it per
  signal invites six settings that contradict each other.

---

## 7a. Parity

Everything the CLI can express is now reachable over HTTP except durable
collection and checkpointing. `suppress:` and `output:` in particular were
accepted by the API, validated, and ignored — the same silent-config failure
as `what.packs` and the `output:` block before them, which is now three
instances of one pattern: **a config key that is parsed but never read.**

Worth a standing check when adding any campaign key: something must fail if it
is misspelled, and something must change if it is right.

A second standing check, from the same work: **a terminal status must not
overstate.** `done` has to mean finished *and* successful, or it is the same
dishonesty as a rejection reason that blames a business for a network error.
Hence `partial` and `interrupted` beside it.

A third, from the fourth testing round: **a guard added for one input has to be
applied to every input of that kind** — and the reliable way to do that is to
put it where the resource is used, not where the config is parsed. Source paths
were confined from the start; suppression lists named by the same campaign were
not, and `suppress: {lists: [/etc/passwd]}` was read over HTTP.

Patching that one key would have left the same trap for the next key. The check
now lives at the point a file is opened (`kerb/paths.py`), so a campaign option
nobody has written yet is covered by default. `tests/test_paths.py` asserts
exactly that, with a path from an imaginary future key.

One naming collision resolved while wiring this: `limits.attempts` is how many
times a durable *unit* is retried; a source's `retries` option is how many times
one HTTP call is retried inside a single attempt. One name for both made
`retries: 1` mean two different things.

## 8. Recommendation

Ordered by value per unit of work, and by whether the evidence is real rather
than imagined:

| | Item | Cost | Evidence | Status |
|---|---|---|---|---|
| 1 | Fix multi-pack truncation + dead `output:` block | small | verified bugs | **done** |
| 2 | `chain_size`, `name_script`, `contactable`, `review_velocity` | small | free signals; one explicitly requested | **done** |
| 5 | Output shaping (`columns`, `min_score`, `top`, `split_by`) | small | already parsed, did nothing | **done** |
| 3 | Suppression lists + `--since` delta | medium | four hand-rolled scripts | **done** |
| 4 | Geo packs + run order | small | twenty-one hand-maintained files | **done** |
| 6 | `requalify` | medium | one hand-rolled script, and it over-deleted | **done** |
| 7 | Confidence weighting | small | internal inconsistency | **done** |

Everything on this list is now built, including `rating_band` (§4.5), output
`template:` (§5.4) and score `bands:` (§6.2).

Two of those three turned out to be more than cosmetic once implemented:

- **`rating_band` is really about volume.** A 5.0 from two reviews and a 4.6
  from four hundred are not the same claim, so review count sets the
  confidence and a rating below a handful of reviews is withheld rather than
  banded from noise. A source reporting out of 10 is refused rather than
  silently marked excellent.
- **`template:` needed validation more than it needed features.** A typo that
  blanks a column is bad in a spreadsheet and unforgivable in an email that
  reached a customer, so every field a template names is checked against the
  real field and signal names *before* the run starts.

Items 1, 2, 4 and 5 are a day's work between them and remove most of the manual
labour visible in the predecessor. Item 3 is the one that changes what the tool
*is*: without memory, kerb is a search; with it, kerb is a pipeline someone can
run every week.
