# Alpha test: the web UI

Write down anything that fails or reads wrong, with the URL, the device and browser, and a
screenshot if you can.

## 0. First start

```sh
git pull && docker compose up -d --build
docker compose logs jobalerts | grep "setup code"     # the one-time code
curl -s http://<jobalerts-host>:8765/healthz          # expect: ok
```

- [ ] `http://<jobalerts-host>:8765/` sends you to `/setup`; the code from the log and a password log you in
- [ ] A wrong password a few times makes you wait; `jobalerts set-password` resets it
- [ ] Log out, log in from your phone: the pages fit the screen, the nav scrolls sideways

## A. Jobs

- [ ] Recommended lists matches with fit, step-up and pay index; the filters narrow it
- [ ] Save / Dismiss / Applied work in the list and on a job's page; dismissed jobs leave
      Recommended and the Home Assistant sensor
- [ ] A job's page shows the summary, red flags, stories, resume and the pay breakdown
- [ ] With Settings → Connections → address set, tapping a phone alert opens that job's page
      (Open posting still goes to the employer)

## B. Boards

- [ ] Every watched company appears with its status; a failing one is at the top with its error
- [ ] "Find its job board" for a company you know finds its board; Add puts it on the list and
      it starts polling (Next fills in)
- [ ] Turn off / Turn on, tier and Remove change `data/companies.yaml` and keep its comments

## C. Stories

- [ ] Editing a story updates "What the AI sees" as you type; TODOs and talk tracks stay out
- [ ] Save keeps the file's comments when you didn't change the fields
- [ ] After a change, "Re-score" redoes the recent scores (progress shows; nothing is pushed)

## D. Settings

- [ ] Your situation: salary, home, housing cost, household and stay-or-move save and change the
      pay indexes at once (`jobalerts pay` agrees)
- [ ] API keys: a saved key shows only "set"; Test answers for Anthropic and ntfy; Clear works
- [ ] Profile and resumes: edits save; an uploaded resume is listed; Remove moves it to .trash
- [ ] Account: the Home Assistant link opens the map without logging in, and nothing else

## E. Off and on again (desktop mode)

- [ ] Stop it for an hour or more (or sleep the PC), start it: one "Catch-up" push, the best few
      as their own pushes, a "Since you were away" box on the jobs page
- [ ] A second `jobalerts run` on the same data stops with "already running"

## F. The map

Check a few against the command line (`docker compose exec jobalerts jobalerts pay 250000 "<city>"`).
Open a dot's popup with the slider at $250K.

Check a few against the command line (`docker compose exec jobalerts jobalerts pay 250000 "<city>"`).
Open a dot's popup with the slider at $250K.

- [ ] Your home dot (the ringed one) has a break-even equal to your reference salary for renting and buying
- [ ] Albuquerque, Austin and Boston: rent and buy index in the popup match `jobalerts pay` to 0.01
- [ ] San Jose metro: compare with `jobalerts pay 250000 "Santa Clara, CA"` (rent ≈ 1.28, buy ≈ 0.70)
- [ ] A city with its own blend (e.g. Tacoma): the job/`pay` number differs a little from the Seattle
      dot. That's expected; the dot is metro-wide (see *Table & details → About*)
- [ ] Break-even view: moving a metro's salary to its break-even gives an index of 1.00 there
      (pick one metro, read its break-even, set the slider to it in index view)

### The page

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

###  Home Assistant

Webpage card from the README (*Cost-of-living map*).

- [ ] The card shows the map on the desktop dashboard
- [ ] The card works in the phone app (on the home Wi-Fi)
- [ ] `?theme=dark` matches a dark dashboard; without it the page follows the device setting
- [ ] Two cards with different presets (e.g. `?view=breakeven&housing=buy` and
      `?view=index&salary=250000`) each open in their own view
- [ ] If you open HA over https (Nabu Casa or a proxy): note what you see. The browser will block
      the plain-http page (see the README for the TLS proxy fix)

###  Keeps working

- [ ] A new top match shows up on the map after the next dashboard refresh
- [ ] `docker compose restart`: the map is back within a minute
- [ ] `jobalerts map --out data/map.html` writes a file that opens offline (no jobs? check the DB path)

## Known limits in this alpha

- US metros only; jobs abroad are listed, not mapped. Non-metro areas (state-level prices) have no dot.
- Dots are metro-wide: no city blend, no city taxes.
- Needs internet in the viewing browser for Leaflet and the state outlines (cdnjs, jsDelivr).
- No login: anyone on the LAN who can reach port 8765 can see the page (your top jobs and pay
  numbers). Don't forward the port to the internet.
