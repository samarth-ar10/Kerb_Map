# Kerb Map

**On-street parking, read from the cameras that already see the kerb.**

Drop a pin or enter a postcode. For each nearby stretch of kerb the page says four things: is it a legal space, is it free or occupied, when was it last seen, and what evidence that rests on. Every answer is a receipt: the frames, the decision trail, and a confidence. "Cannot tell" and "not seen" stay on the page.

## The idea

Each side of a road is a line. Along that line we store where a car may stop, which metres a camera actually saw, and whether those metres were occupied, free, or unclear. A free space is a legal, seen, empty gap of about one car length (5.5 m). Unseen kerb stays unknown. Unknown is a different answer from empty.

A passing dashcam is good at parked cars and weak at empty gaps. A fixed street camera watches the same kerb for minutes, so it can tell a parked car from a car waiting at a red light. The pin query merges both. A newer sighting of the same metres wins. Street cameras keep their stretch fresh. Dashcams cover other roads, then go stale.

## How a receipt is made

Cheap checks run first. Cosmos is last, and only on the gaps those checks cannot settle.

1. **Dashcam.** Take a keyframe about every 8 m. Read the YOLO boxes already stored with the clip. Build parked-car rows and the gaps between them. Reject junctions, crossings, hydrants, bus stops, and anything shorter than a car, using OpenStreetMap. Ask Cosmos only about an unclear gap: free, occupied, not a space, or cannot tell.
2. **Street camera.** Draw the kerb once on a single frame. Track cars. A car is parked only if it sits on the kerb strip for longer than a light cycle, about two minutes. Cars in the lane are traffic. Write the occupied intervals and the metres in view.
3. **Query.** Filter receipts by state and side, keep those near the pin, and attach the evidence. One receipt opens from the first gate to the final word.

## What we already measured

Rehearsed on the public Toronto PIE dashcam set (85 minutes, 6 drives). Labels were frozen first. A fresh held-out set of 9 videos was run once.

| | Result |
|---|---|
| Real legal space, when we answered | 0.84 (38/45) |
| Free vs occupied, on those spaces | 0.895 (34/38) |
| Right when we said "free" | 0.53 (8/15) |
| Pin error, median | about 3 m |
| Slots sent to Cosmos | 2.3% |

The false spaces were driveways, kerbs nobody parks on, a corner, and a hydrant. That "free" number is why the street camera is in the product.

## What the footage allows

The event library stores a location as one word, with no GPS and no clock. Boxes are already saved every 5 seconds, so the demo reads those boxes and does not re-process hours of video. A pin works when the dashcam file carries GPS, or when a street camera's position is set once by hand. The page reports the metres a camera covered, and no more.
