# Autonomous Play Loop

`scripts/loop/` plays any Cities: Skylines map on its own and stops only for what needs a
planner. Every decision comes from game data through the typed mayor tools; nothing in the loop
names a city, a road or a coordinate.

```powershell
python .\scripts\loop\play.py 8 60 --reserve 8000
```

`8` is the number of wakes, `60` the seconds each wake runs the simulation at speed 3. The
local-model cross-check is off by default; add `--model` to turn it on (`--no-model` is still
accepted and is the default). Each wake runs as its own process
(`python -m loop.cycle`), so a crash or an edited module never carries over to the next wake.

## One wake

1. **Simulate and read** (`wake.py`): run the simulation, pause, read `observe_city`. A town no
   road leads into yet keeps its clock held, so upkeep does not run while nothing can move in. The loop always uses
   the controller's own values. Only with `--model` does a local model (`scripts/qwen_crosscheck.py`)
   read the same result so that every answer is compared with the controller's value; a model that
   is not running is skipped. Without `--model` the wake report says `"model": "off"`.
2. **Grow** (`growth.py`), for residential, commercial and workplace:
   - allowance: the pacing of `inspect_growth_capacity`, doubled when last wake's blocks were
     absorbed (up to 12), and a few blocks when vacancy that never builds holds the pacing back;
   - blocks: `propose_zoning_blocks` (neighbourhood use within distance rules; the city plan overrides);
     a block a school, park or utility stands on in part is proposed too when 8 or more of its
     cells are free, and only those cells are zoned; a block whose zoning fails is never proposed
     again;
   - workers short (unemployment at or below 3% while both workplace and residential demand stay
     high): homes are zoned each wake and no new job zoning, since new jobs would stand short of
     workers too, and homes may open new land below the usual expansion demand;
   - homes with no block left to zone at all open new land from residential demand 30 rather
     than the usual 40: the land they were zoned on is being built on;
   - jobs short (unemployment at 6% or more while workplace demand stays high): enough workplace
     blocks are kept ready to build on, three for each point of unemployment from 6% (at most
     12), counting the empty workplace land already zoned; offices go first when office land
     stands less vacant than industrial land;
   - no block left: built-up low-density blocks become high density, centre first;
   - still none, demand is high, every home has water and power, and cash is at least 15,000:
     `propose_expansion_areas` picks free land; an access road (homes in its path are cleared; a
     piece the ground refuses turns toward any open direction), an overpass over a railway or road
     when needed (`propose_network_crossing`), and the district laid in the layout the land suits
     (`mayor/layouts.py`: corridor for a narrow strip, ring for sloping land, industrial for
     workplaces, grid for compact flat land, else fishbone). A fishbone grows a few collector steps
     a wake (a refused step is steered, shortened, or laid as two halves that follow the ground);
     any other layout is laid from its road graph ten roads a wake (`loop/district.py`). An open
     district goes on only while its use still wants land, and a district for another use (industry
     beside homes) starts on land where that use may grow;
   - land beyond a river (`loop/bridge.py`, `mayor/bridge.py`): the cheapest crossing from the
     road ends nearest it, a ground road to the bank and a bridge from 40, 80 or 120m back from the
     water (a steep bank is crossed raised) to 40m beyond it, every piece previewed and priced first;
     a town that earns more than it spends takes the smallest loan that covers it (`take_loan`),
     one in deficit waits; the first district across is a bridgehead, a closed three-by-three grid
     where the bridge lands. A plan entry with `crossTo` asks for that land next;
   - a new city with no road yet: the highway's exit and entrance stubs are joined as a player
     drags them: each runs on into the map and a cross road joins the two ends (roads already run
     on from both stubs are joined, not rebuilt), and the first fishbone grows from there. Every
     road of an attempt is previewed, and checked against the others for room, before the first
     is built; an attempt that still fails is bulldozed at the game's refund, and attempts are
     capped. Zoning is high density on paved roads once unlocked.
3. **Clear ruins**: abandoned, burned and collapsed buildings in one change, and buildings no
   local road reaches (the road they grew on is gone; the game raises no problem for them).
   Growables stacked on one lot (same position and angle) are cleared down to the newest, up to
   300 a wake, and the report names the lots: Bridge commands that ran beside the simulation
   thread left such stacks (now every command runs on the simulation thread), and a few lots still
   grow a new building over a standing one.
4. **Utilities** (`utilities.py`), once a road leads into the town (built earlier they only cost
   upkeep). Buildings without power or water are first split by `diagnose_utility_shortfall`
   into those the network reaches yet gives nothing (the supply is short: a plant, or the water
   budget back to full; a short supply reads well below capacity, as the game counts only what it
   delivers) and those beyond it, which get lines and pipes.
   Homes are not zoned inside a noisy facility's noise radius (a wind turbine pollutes nothing yet
   keeps homes awake); rising noise goes to the planner with `diagnose_noise`'s named sources.
   A city without water gets its first system: an intake/outlet pair on the nearest shore up- and
   downstream of the town, on water at least 2m deep that reaches each port within 8m, one branch
   from each facility's pipe port, and a shared trunk to the town through a junction whose pipes
   part widely (the plan is previewed whole, built step by step and continued by the next wake if
   a wake stops). An intake the river has since left (powered, yet without a water source on two
   wakes running: one just placed has not pumped yet) is bulldozed and its outlet kept; the next wake sites a new one and pipes it to the
   nearest joint of the pipes already laid (they are checked as built, not laid again). Outlets are never moved:
   the game gives an outlet a source wherever it stands. A grid with no capacity, running near full, or starved (buildings dark beside
   powered ground: the game counts only the power it delivers as used) gets the plant
   `compare_service_options` ranks first, sited by `propose_power_plant_candidates`. While the
   city runs a deficit, a water system far below its capacity has its budget turned down (power
   keeps a full budget); a budget put back to full waits for no cooldown.
5. **Problems** (`services.py`): each problem from `assess_problem_remedies`; facilities chosen by
   `compare_service_options` for the need (coverage, capacity, fillsUp); a placement that did not
   remove its cluster is marked ineffective for that need and skipped next time. Ruins cleared in
   the wake are counted by kind and by the problems left on them; ten or more burned within 300m
   of one another (a fire is out before the next wake reads it) get a fire station there first,
   and so does a smaller burned group more than 1,500m by road from the nearest stations (the new
   station's site must itself be within that drive).
   An economy problem still rising gets its lever where the loop has one: shops short of customers
   have the empty commercial blocks by them zoned for homes (offices where homes may not go, and
   only while jobs are wanted and workers are not short); a shop block with eight or more empty
   cells beside its shops has only those cells rezoned (`vacantCellsOnly`). A rise in idle shops
   goes to the planner only past 5% of the city's shops, any other rising economy problem only
   past 1% of the city's buildings, and
   industry short of goods gets a Forestry or Farming district (`loop/industry.py`) painted over
   the industrial blocks on forest or fertile ground, one at a time; the planner hears of either
   only when nothing is left to do.
6. **Traffic** (`traffic.py`): hotspots that persist get the remedies `propose_traffic_remedies`
   ranks (downstream relief, transit, junction trials, widening, a bypass from
   `propose_bypass_roads`), one action a wake, each judged afterwards; kinds go in the order of
   their success rate in this city, a kind that failed twice at a hotspot is not tried there again,
   and a hotspot whose last remedy is still to be judged gets no other. Two more kinds work on the
   roads around the jam: a lane drop (a narrower stretch between wider roads of the same kind on
   the queue's way out) is widened to match them, and a quiet road running beside a jammed exit
   is widened to take a share of it. A bypass starts upstream of the hotspot, where its traffic enters, and is
   built piece by piece: homes and small utility buildings (water towers, wind turbines) under a
   piece are cleared after their own bulldoze preview, and a piece that fails takes down the
   pieces already built. Freight from the industry gets a cargo train terminal: beside the railway
   when a lot fits, else by the industry with a tunnel to the railway (`loop/cargo_remote.py`),
   whose ramp up into the railway is laid in two pieces; a failed try counts against the three
   allowed for 40 wakes only, as the town around the way changes, and a terminal that no longer
   stands is let go, so the industry may get one again where it needs it, and the track it laid
   is removed from its dead end inward, so no later line runs into it underground.

Every change runs under the protocol in `protocol.py`: checkpoint, regression baseline, change,
settle, verify. The checkpoint save is taken once per phase of a wake (utilities, growth,
clearing, services, budgets, traffic) and reused by that phase's later changes, which still get
their own baseline and verification; a failed change is recovered from the city before its phase
began. Each wake also deletes a few of the oldest agent checkpoints beyond the newest 20. A
refused change is a failed action, not a crash. Ground roads are left to the game
like a player's: built where the native tool builds them, cut or filled as the game shapes the
ground, and refused only when the terrain the game draws covers them.

## When it stops

A capability unlocks, cash falls under the reserve, a change or its regression check fails, the
local model disagrees (only with `--model`), utility capacity runs short, buildings stay without water or power,
a problem is unmapped or keeps rising, a service cannot be placed, a hotspot's executable remedies
are spent and the remedy it still needs is structural (a new access road, a redesign), every way into new land is refused, or
no crossing reaches land across water. A collector the ground refuses does not stop the loop: the
next wake skips that landing and takes another way in.

Growing an existing water system with a further intake/outlet pair is still a planner's choice.

## The city profile

What the loop decides or learns about one city is kept in `.runtime/cities/<instanceId>/`, keyed by
the game's city instance id, so the same loop picks up the right state on any map. Git ignores
`.runtime/`: a city's layout is its player's and never reaches the repository.

`profile.json` is the file a planner edits:

```json
{
  "plan": [{"minX": -600, "maxX": 0, "minZ": 0, "maxZ": 500, "kind": "workplace",
            "reason": "keep industry apart from homes"}],
  "notes": ["land across the river needs a bridge before it can grow"],
  "settings": {"reserve": 8000}
}
```

A planned area overrides the use a block's neighbourhood suggests, for zoning and for new land.
The other files (`growth.json`, `services.json`, `traffic.json`, …) are the loop's own state, and
`play-log.jsonl` records every wake.
