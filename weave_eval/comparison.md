# Kerb rules vs human labels

One change per variant versus shipped; each cell is right / system said (precision) or right / human said (recall). `exact` = system state equals the human label (cannot_tell counts as wrong). Computed offline from the stored per-spot states; no pipeline re-run, no W&B. Tuning drives = set01-03, held-out drives = set04-06. Both labellers answered some set01-03 spots, so the tuning half is no longer purely the set the rules were tuned on. Two labellers, ~55 spots: one spot moves a cell by 8-30 points.

## all (n=53)

| variant | free P | free R | occupied P | occupied R | real-space P | real-space R | exact |
|---|---|---|---|---|---|---|---|
| shipped | 5/6 | 5/14 | 7/11 | 7/13 | 12/17 | 12/27 | 17/53 |
| +gap 0.85 | 8/9 | 8/14 | 7/11 | 7/13 | 15/20 | 15/27 | 20/53 |
| +travel-lane row | 5/6 | 5/14 | 7/8 | 7/13 | 12/14 | 12/27 | 23/53 |
| +OSM no-parking travel lane | 5/6 | 5/14 | 7/11 | 7/13 | 12/17 | 12/27 | 18/53 |
| all three (chosen) | 8/9 | 8/14 | 7/8 | 7/13 | 15/17 | 15/27 | 27/53 |

## tuning (set01-03) (n=28)

| variant | free P | free R | occupied P | occupied R | real-space P | real-space R | exact |
|---|---|---|---|---|---|---|---|
| shipped | 1/1 | 1/6 | 5/7 | 5/8 | 6/8 | 6/14 | 8/28 |
| +gap 0.85 | 3/3 | 3/6 | 5/7 | 5/8 | 8/10 | 8/14 | 10/28 |
| +travel-lane row | 1/1 | 1/6 | 5/6 | 5/8 | 6/7 | 6/14 | 9/28 |
| +OSM no-parking travel lane | 1/1 | 1/6 | 5/7 | 5/8 | 6/8 | 6/14 | 9/28 |
| all three (chosen) | 3/3 | 3/6 | 5/6 | 5/8 | 8/9 | 8/14 | 12/28 |

## held-out (set04-06) (n=25)

| variant | free P | free R | occupied P | occupied R | real-space P | real-space R | exact |
|---|---|---|---|---|---|---|---|
| shipped | 4/5 | 4/8 | 2/4 | 2/5 | 6/9 | 6/13 | 9/25 |
| +gap 0.85 | 5/6 | 5/8 | 2/4 | 2/5 | 7/10 | 7/13 | 10/25 |
| +travel-lane row | 4/5 | 4/8 | 2/2 | 2/5 | 6/7 | 6/13 | 14/25 |
| +OSM no-parking travel lane | 4/5 | 4/8 | 2/4 | 2/5 | 6/9 | 6/13 | 9/25 |
| all three (chosen) | 5/6 | 5/8 | 2/2 | 2/5 | 7/8 | 7/13 | 15/25 |

## labeller: Atharva (n=31)

| variant | free P | free R | occupied P | occupied R | real-space P | real-space R | exact |
|---|---|---|---|---|---|---|---|
| shipped | 2/3 | 2/6 | 5/6 | 5/8 | 7/9 | 7/14 | 11/31 |
| +gap 0.85 | 3/4 | 3/6 | 5/6 | 5/8 | 8/10 | 8/14 | 12/31 |
| +travel-lane row | 2/3 | 2/6 | 5/6 | 5/8 | 7/9 | 7/14 | 12/31 |
| +OSM no-parking travel lane | 2/3 | 2/6 | 5/6 | 5/8 | 7/9 | 7/14 | 11/31 |
| all three (chosen) | 3/4 | 3/6 | 5/6 | 5/8 | 8/10 | 8/14 | 13/31 |

## labeller: Builder C (n=28)

| variant | free P | free R | occupied P | occupied R | real-space P | real-space R | exact |
|---|---|---|---|---|---|---|---|
| shipped | 3/3 | 3/10 | 2/5 | 2/5 | 5/8 | 5/15 | 8/28 |
| +gap 0.85 | 5/5 | 5/10 | 2/5 | 2/5 | 7/10 | 7/15 | 10/28 |
| +travel-lane row | 3/3 | 3/10 | 2/2 | 2/5 | 5/5 | 5/15 | 14/28 |
| +OSM no-parking travel lane | 3/3 | 3/10 | 2/5 | 2/5 | 5/8 | 5/15 | 9/28 |
| all three (chosen) | 5/5 | 5/10 | 2/2 | 2/5 | 7/7 | 7/15 | 17/28 |

## Street camera: old track rule vs position-based (agreement with human labels)

| split | n | old rule | position-based | fixed | broken |
|---|---|---|---|---|---|
| dev (A3, A6) | 40 | 25/40 | 34/40 | 10 | 1 |
| held-out (A1, A2, A4) | 51 | 41/51 | 45/51 | 4 | 0 |

Not logged to Weave: the eval files hold confusion matrices, not per-item before/after states.
