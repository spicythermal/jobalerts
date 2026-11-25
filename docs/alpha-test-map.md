# Alpha test: cost-of-living map

Build: Phase 16 (see PLAN.md). Write down anything that fails or reads wrong, along with the URL
(it holds the view, housing and salary), the device and browser, and a screenshot if you can.

## 0. Deploy

```sh
git pull && docker compose up -d --build        # the compose file now publishes port 8765
docker compose logs jobalerts | grep -i map      # expect: "Cost-of-living map on http://0.0.0.0:8765/"
curl -s http://<jobalerts-host>:8765/healthz     # expect: ok
```

- [ ] `http://<jobalerts-host>:8765/` opens in a desktop browser on the LAN
- [ ] Within a minute or so the "being built" placeholder is replaced by the map (reload)

## 1. The numbers

Check a few against the command line (`docker compose exec jobalerts jobalerts pay 250000 "<city>"`).
Open a dot's popup with the slider at $250K.

- [ ] Your home dot (the ringed one) has a break-even equal to your reference salary for renting and buying
- [ ] Albuquerque, Austin and Boston: rent and buy index in the popup match `jobalerts pay` to 0.01
- [ ] San Jose metro: compare with `jobalerts pay 250000 "Santa Clara, CA"` (rent ≈ 1.28, buy ≈ 0.70)
- [ ] A city with its own blend (e.g. Tacoma): the job/`pay` number differs a little from the Seattle
      dot. That's expected; the dot is metro-wide (see *Table & details → About*)
- [ ] Break-even view: moving a metro's salary to its break-even gives an index of 1.00 there
      (pick one metro, read its break-even, set the slider to it in index view)

## 2. The page

- [ ] Pay index ↔ Break-even salary switches the colors, legend title and the line under the bar
- [ ] Rent ↔ Buy switches; the buy view is redder in coastal California and the Northeast
- [ ] The salary slider recolors smoothly from $100K to $400K; hidden in break-even view
- [ ] Hovering a dot shows its name and number; clicking shows the full breakdown
- [ ] Job diamonds (top matches) and rings (step ups) sit at the right metro; several jobs in one
      metro don't hide each other; their popups link to the posting
- [ ] Jobs checkbox hides and shows the job markers
- [ ] *Table & details*: jobs abroad are listed (with why they aren't on the map); the metro table
      sorts by each column and the filter box narrows it; *About* names the sources and dates
- [ ] Reload: the page remembers the last view, housing, salary and jobs setting
- [ ] Legend: open on desktop; collapsed on a phone, opens with a tap on its title
- [ ] State outlines appear behind the dots (they load a moment after the dots)

## 3. Home Assistant

Webpage card from the README (*Cost-of-living map*).

- [ ] The card shows the map on the desktop dashboard
- [ ] The card works in the phone app (on the home Wi-Fi)
- [ ] `?theme=dark` matches a dark dashboard; without it the page follows the device setting
- [ ] Two cards with different presets (e.g. `?view=breakeven&housing=buy` and
      `?view=index&salary=250000`) each open in their own view
- [ ] If you open HA over https (Nabu Casa or a proxy): note what you see. The browser will block
      the plain-http page (see the README for the TLS proxy fix)

## 4. Keeps working

- [ ] A new top match shows up on the map after the next dashboard refresh
- [ ] `docker compose restart`: the map is back within a minute
- [ ] `jobalerts map --out data/map.html` writes a file that opens offline (no jobs? check the DB path)

## Known limits in this alpha

- US metros only; jobs abroad are listed, not mapped. Non-metro areas (state-level prices) have no dot.
- Dots are metro-wide: no city blend, no city taxes.
- Needs internet in the viewing browser for Leaflet and the state outlines (cdnjs, jsDelivr).
- No login: anyone on the LAN who can reach port 8765 can see the page (your top jobs and pay
  numbers). Don't forward the port to the internet.
