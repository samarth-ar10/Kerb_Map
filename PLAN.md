# On-street parking availability

A person enters a postcode or drops a pin. The site reports how many on-street spaces are free on the nearby roads at a chosen time, and when each stretch of kerb was last seen.

The footage is the dashcam and CCTV library in the team VSS instance: [team-5-vss dashboard](https://team-5-vss.thecosmoslabs.com/dashboard) (Vast VSS Blueprint, Video Search & Summarization). That catalog is behind login. The first build task is to list every clip, tag it as dashcam or fixed CCTV, and record which road it shows.

## What a space is

On-street parking is a length of kerb, not a painted bay. Each road is stored as two lines, one per side. Along each line there are three kinds of interval:

- **Legal.** Where a car may stop at that time of day. Double yellows, bus stops, junctions, dropped kerbs, and driveways stay out. Single yellows and permit bays move in and out with the clock.
- **Seen.** The metres a camera actually had in view. Unseen kerb stays unknown.
- **Occupied.** The metres where a stationary vehicle is sitting.

A free space is a legal, seen, unoccupied gap of about 5 metres. The number for a road is how many of those gaps fit on each side, plus the time that side was last seen.

A postcode only selects the nearby kerb lines. The page shows the road, the side, the free count, how many metres were visible, and the age of the sighting.

## Footage

Both sources write the same record: kerb id, from–to metres, occupied or empty, time, confidence.

**CCTV** keeps one stretch live. Use it where a fixed camera looks along the kerb.

1. Survey the camera position.
2. Project the road surface from image pixels onto that kerb line, once, from camera height, angle, and a few measured points on the street.
3. Detect vehicles. Keep the ones that stay still in the world for a second or two. Those are parked. Cars moving along the lane are traffic.
4. Drop each parked car’s footprint onto the kerb as an occupied interval, and mark the visible stretch as seen.
5. Repeat every few seconds. Store the intervals. Drop the frame.

One camera covers the near kerb and loses the far end to occlusion and perspective. Plan on one camera per stretch that must stay live.

**Dashcam** records a single pass.

1. Keep GPS, heading, speed, and time with the video.
2. Detect parked vehicles and the kerb edge. Keep those detections and the track.
3. Snap the drive to the road centreline before deciding which side of the street the car was on.
4. Project each detection onto the left or right kerb. The bottom of the vehicle in the image, on a flat-road assumption, gives the distance.
5. Write occupied intervals only where the kerb was in frame. A gap counts as free only when the camera saw that kerb and no parked car was there.

The along-street error has to stay inside one car length, about 5 metres. A car on the wrong side, or 10 metres down the road, makes the free count wrong even when the detector is right.

A dashcam result expires quickly. On a busy street a space seen five minutes ago is a weak claim. Show it as last-seen, and let a later CCTV view of the same metres replace it.

## Query

A newer sighting of the same metres replaces an older one. CCTV keeps refreshing the metres it can see. Dashcams fill roads that have no camera, then go stale.

For a postcode at a chosen time:

- load kerb lines within a short radius
- apply the parking rules for that clock time
- subtract occupied intervals that are still fresh
- count car-length gaps on the seen, legal kerb
- return, per road side: free count, metres covered, last seen

Until a stretch has been seen, report it as unknown. An empty answer and a “not seen” answer are different.

## Build order

1. Log in to the VSS dashboard and make a clip table: file, source (dashcam or CCTV), road, time range, whether GPS or a camera position exists.
2. Hand-draw both kerbs and the restrictions for one street that appears in that table.
3. Run one dashcam clip of that street and check that each parked car lands on the correct side, within a few metres of where it really is.
4. Run one CCTV clip of the same kerb and check that its intervals land on the same line.
5. Publish a page that turns a postcode into a free count, with coverage and last-seen time.
6. Add more roads only after those two clips agree.

## Privacy

Point the processing at bays and kerbs. Occupancy does not need faces or number plates, so do not collect them. Keep raw video only as long as the clip library already does, and store the derived intervals as the product.
