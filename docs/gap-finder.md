# Gap Finder Methodology — v1.0 Technical Description

This document specifies the **gap-finder** methodology used in Project Galtea to identify **likely-but-not-yet-observed** star systems in the Eotchorts region of Elite Dangerous using a combination of:

1) **Name-space inference** (sequence “holes” in discovered system names), and  
2) **Spatial anchoring** (restricting inference to naming families that are active within a **discovered-only** neighborhood around the capital system).

The output is a **ranked candidate list** intended for **in-game Galaxy Map checking** and/or **EDSM validation**, not a claim that any candidate definitively exists.

---

## 1. Design Principles

### 1.1 What this method can and cannot do

**Can do**
- Identify **missing identifiers** (gaps) in **dense, locally active** naming sequences.
- Prioritize candidates by **confidence signals** derived from observed neighbors and local family activity.
- Produce reproducible, explainable rankings for flight planning and verification.

**Cannot do**
- Derive coordinates for undiscovered systems (they have none).
- Prove existence. A “gap” could represent:
  - a system that exists but is undiscovered by players / not in your dataset,
  - a system discovered but not submitted to aggregators,
  - a number never used by Stellar Forge in that family.

### 1.2 Two-tier approach (recommended)

- **Tier A: Name-space gap inference (global)**  
  Use discovered system names to infer “holes” within naming families.

- **Tier B: Spatially anchored gap inference (capital neighborhood)**  
  Use **coordinates of discovered systems only** to define a neighborhood (e.g., 150 ly around capital), identify which naming families are active there, then generate gaps **only within those families**.

Tier B reduces irrelevant candidates and aligns better with operational exploration around the capital.

---

## 2. Data Dependencies

### 2.1 Authoritative store: SQLite working database

The gap finder operates against the **working SQLite database** (e.g., `data/working/galtea_working.sqlite`) which is built from an immutable read-only source sector DB and optionally enriched with journal ingestion.

**Minimum required fields:**
- `system_name` (TEXT)
- `system_address` (INTEGER/INT64 preferred; optional but strongly recommended)
- `x`, `y`, `z` (REAL) for discovered systems (required for spatial method)

**Assumed sources in the Galtea pipeline:**
- `systems_in_radius` (preferred): contains discovered systems around the capital for the current configured radius, often with distance and coords
- OR `systems` (or similar): base systems table with coordinates

The gap finder is **read-only** with respect to the source sector DB; it may create output files and, if needed, temporary working tables/views in the working DB.

### 2.2 Optional enrichment sources

**Commander journals** (optional, not required for the core gap finder)
- Used to ingest newly discovered systems not present in the source DB and/or confirm existence
- Not required to run the gap methodology, but improves coverage

**EDSM API** (optional validation)
- Used to validate whether a candidate name already exists in EDSM
- Not required to generate candidates; used to reduce wasted flight time

---

## 3. Terminology and Parsing

### 3.1 System name format

This method targets the common Elite Dangerous procedural naming format as observed in Eotchorts:

`Eotchorts FG-X d1-318`

We define:

- **prefix / sector name**: `Eotchorts`
- **family (a.k.a. stem)**: `FG-X d1`
- **suffix number (n)**: `318`

### 3.2 Parsing rule

Only consider system names that match:

- Start with `Eotchorts ` (sector prefix)
- Followed by a **family** token and a numeric suffix `-<n>`

A defensible regex for family + numeric suffix is:
^Eotchorts\s+(?P<family>[A-Z]{2}-[A-Z]\s+[a-z]\d+)-(?P<n>\d+)\s*$


Examples:

- `Eotchorts FG-X d1-318` → family=`FG-X d1`, n=318  
- `Eotchorts EG-X d1-270` → family=`EG-X d1`, n=270  
- `Eotchorts LX-S c4-31`  → family=`LX-S c4`, n=31

Non-matching names should be logged and ignored (or handled in a separate parser extension).

---

## 4. Tier A: Name-Space Gap Finder (Global)

Tier A is name-only; it does not require coordinates.

### 4.1 Input set

- All discovered system names in the working DB that match the parsing rule for the sector prefix.

### 4.2 Grouping

Group systems by `family`.

For each family:
- Collect the sorted unique list of suffix numbers: `N = sorted({n_i})`

### 4.3 Dense segment detection

We do not want to generate candidates across extremely sparse ranges. Define **dense segments** within each family by splitting the sorted list when gaps are too large.

Parameters:
- `max_step` (default 25): split a segment when `(n[i+1] - n[i]) > max_step`
- `min_support` (default 8): only segments with at least this many observed members can generate candidates

Algorithm:
1. Walk sorted `N`
2. Start a new segment at the first element
3. Continue adding to segment while delta ≤ `max_step`
4. If delta > `max_step`, finalize segment and start a new one

Each segment has:
- `seg_min`, `seg_max`
- `seg_observed_count`
- `seg_observed_values` (set for membership tests)

### 4.4 Candidate generation within segments

For each eligible segment:
- Consider all integers `m` in `[seg_min, seg_max]` where `m` is not in observed values.

Create candidate system name:
- `candidate_system_name = f"Eotchorts {family}-{m}"`

### 4.5 Candidate confidence features

Even without coordinates, we can rank by confidence:

#### A) Bracketing confidence (best signal)
Let:
- `L` = largest observed n < m in the segment
- `U` = smallest observed n > m in the segment

If both exist, candidate is **bracketed**:
- `bracketed = 1`
- `gap_width = U - L`

Intuition: `L=12, U=14, missing=13` is strong evidence.

A simple bracket score:
- `bracket_score_raw = 1 / gap_width` (0 if not bracketed)

#### B) Local neighbor density
Count how many observed values exist within a small window around `m`:

- `neighbor_k` (default 2)
- `local_neighbor_hits = count(n in observed where |n - m| <= neighbor_k)`
- `density_score = clamp(local_neighbor_hits / (2 * neighbor_k), 0, 1)`

### 4.6 Priority score (Tier A)

A defensible weighted score for Tier A:
priority_score =
0.55 * density_score

0.45 * bracket_score_norm

Where `bracket_score_norm` can be derived as either:
- **min-max normalization** across all candidates, or
- **saturating transform**:
  - `bracket_score_norm = clamp(bracket_score_raw * 4, 0, 1)`
    - gap_width=2 → 0.5*4=2 → 1.0 (max confidence)
    - gap_width=4 → 0.25*4=1.0 (still maxed)
    - adjust multiplier if desired

The saturating transform keeps behavior stable and explainable.

### 4.7 Output

`gap_candidates.csv` columns (recommended):
- `family`
- `missing_n`
- `candidate_system_name`
- `seg_min_n`, `seg_max_n`, `seg_observed_count`
- `bracketed`, `lower_neighbor_n`, `upper_neighbor_n`, `gap_width`
- `local_neighbor_hits`, `density_score`
- `bracket_score_raw`, `bracket_score_norm`
- `priority_score`
- `notes`

Sort:
- `priority_score DESC`, then `family ASC`, then `missing_n ASC`

---

## 5. Tier B: Spatially Anchored Gap Finder (Capital Neighborhood)

Tier B anchors to the **real discovered neighborhood** around the capital system using coordinates.

### 5.1 Neighborhood definition (discovered systems only)

Select discovered systems with valid coordinates where:

- `dist_to_capital <= neighborhood_radius_ly` (default 150)

Distance formula:
dist = sqrt((x-x0)^2 + (y-y0)^2 + (z-z0)^2)

Where `(x0,y0,z0)` is the capital’s coordinates (from the DB row for the capital system).

**Important:** This step includes **all naming families** inside the radius. It is **not** constrained to the capital’s family.

### 5.2 Extract neighborhood families

From the neighborhood systems, parse `family` and `n` as in Section 3.

Compute per-family neighborhood stats:
- `family_count_in_radius`
- `min_n`, `max_n`
- `family_min_dist_ly` (min of dist_to_capital)
- `family_median_dist_ly`
- `segment_count`, `densest_segment_count` (using same segmentation rules but over neighborhood members)

Eligibility rule:
- Only families with `family_count_in_radius >= min_support` are eligible for candidate generation.

### 5.3 Generate candidates only within neighborhood-active families

Repeat the Tier A segment/candidate generation, but using only observed values from the neighborhood set.

This ensures:
- You only generate gaps inside families proven to exist near capital.

### 5.4 Additional ranking signals (Tier B)

Tier B candidates receive Tier A confidence plus neighborhood family signals.

#### B) Local family strength (neighborhood support)
Convert `family_count_in_radius` into a score:
family_strength_score = min(1, family_count_in_radius / 200)

(Use 200 as a tunable saturation constant; alternative: quantile normalize.)

#### C) Family proximity proxy (honest spatial)
Use the family’s median distance within the neighborhood:
family_prox_score = clamp(1 - (family_median_dist_ly / neighborhood_radius_ly), 0, 1)

Interpretation: “this family is active close-in,” not “this missing system is close.”

#### D) Boxel-code adjacency (optional)
If you maintain a separate index of boxel frequency near capital, add:

- `boxel_score` normalized 0..1

If not present, set 0 and continue.

### 5.5 Tier B priority score (recommended)

A confidence-first weighted model:
priority_score =
0.45 * bracket_score_norm

0.25 * density_score

0.20 * family_strength_score

0.10 * family_prox_score
(+ optional boxel_score with small weight)

If adding `boxel_score`, either:
- add it as an additive term and clamp to 1, or
- rebalance weights so they sum to 1.

### 5.6 Output

`gap_candidates_spatial.csv` columns (recommended):
- `family`
- `missing_n`
- `candidate_system_name`
- `seg_min_n`, `seg_max_n`, `seg_observed_count`
- `bracketed`, `lower_neighbor_n`, `upper_neighbor_n`, `gap_width`
- `local_neighbor_hits`, `density_score`
- `family_count_in_radius`, `family_min_dist_ly`, `family_median_dist_ly`
- `family_strength_score`, `family_prox_score`
- `boxel_score`
- `bracket_score_norm`
- `priority_score`
- `notes`

Sort:
- `priority_score DESC`, then `family ASC`, then `missing_n ASC`

Also output a family stats file:
- `neighborhood_family_stats.csv`

---

## 6. Validation Layer (Optional, Recommended): EDSM Batch Check

Because candidates are hypotheses, validation reduces wasted flight time.

### 6.1 EDSM purpose

For each candidate system name:
- Query EDSM to determine whether the system is already known in EDSM.
- If found, it’s not “undiscovered,” though it may still be absent in local data.

### 6.2 API dependency

EDSM Systems v1 endpoint:

- `GET https://www.edsm.net/api-v1/system?systemName=<name>&showId=1&showCoordinates=1`

Store results in:
- `gap_candidates__edsm.csv`
- `gap_candidates_spatial__edsm.csv`

Recommended added fields:
- `edsm_found` (0/1)
- `edsm_id`, `edsm_id64`
- `edsm_coords_x`, `edsm_coords_y`, `edsm_coords_z` (if provided)
- `edsm_checked_ts_utc`
- `edsm_msg` (errors/not found)

### 6.3 Caching

Use a local cache (SQLite or JSON) keyed by `systemName` with a TTL (e.g., 14 days) to avoid repeated calls during dev.

---

## 7. Operational Use (In-Game Checking Strategy)

### 7.1 What “not found” means

If `edsm_found=0`, then candidate may be:
- undiscovered,
- discovered but not submitted,
- or invalid/unallocated.

So the flight plan should emphasize:
- **high `priority_score`**
- **bracketed candidates (small gap_width)**
- **families strong and close in the neighborhood**

### 7.2 Grouping for travel efficiency

Produce a grouped view (recommended):
- rank families by their top candidate priority
- within each family, check candidates in priority order
- optionally cluster by nearby discovered anchor systems (those neighbors have coords)

---

## 8. Reproducibility and Configuration

### 8.1 Configuration parameters (suggested TOML)

```toml
[gap_finder]
max_step = 25
min_support = 8
neighbor_k = 2
top_n_candidates = 500

[gap_finder_spatial]
neighborhood_radius_ly = 150
max_step = 25
min_support = 8
neighbor_k = 2
top_n = 500
```

### 8.2 Determinism

To keep runs repeatable:

use fixed sorting

avoid randomness

log excluded families/segments

store script version and config snapshot in run logs

---

## 9. Known Limitations and Mitigations

### 9.1 Unallocated numbers

Some missing numbers may never be used by Stellar Forge for that family.

Mitigation:

prioritize bracketed missing values (small gap_width)

validate via EDSM

validate via in-game search

### 9.2 Dataset incompleteness

Your source DB may be incomplete relative to current galaxy data.

Mitigation:

ingest commander journals (adds discovered-only systems)

validate via EDSM

consider periodic refresh of source DB cut

### 9.3 Parsing gaps from unusual naming patterns

Not all names follow the regex.

Mitigation:

log non-matching names

extend parser if new patterns appear

## 10. Summary of Deliverables

Tier A

Script: scripts/08_gap_finder.py

Output: data/outputs/gap_candidates.csv

Optional: data/outputs/stem_stats.csv

Tier B

Script: scripts/09_gap_finder_spatial.py

Output: data/outputs/gap_candidates_spatial.csv

Output: data/outputs/neighborhood_family_stats.csv

Doc: docs/GAP_FINDER_SPATIAL.md

Validation

Script: scripts/10_edsm_validate_candidates.py

Output: gap_candidates__edsm.csv, gap_candidates_spatial__edsm.csv

Cache: data/working/edsm_cache.sqlite

## 11. Implementation Checklist (Quick)

- Confirm DB table providing discovered systems + coords
- Confirm capital system row exists with coords
- Implement neighborhood selection query (coords-only)
- Implement robust name parsing
- Implement segmentation + candidate generation
- Implement bracketing + density scores
- Implement family strength + proximity proxy (spatial)
- Export candidates + family stats
- (Optional) EDSM batch validation + caching
- Add docs + config sections
