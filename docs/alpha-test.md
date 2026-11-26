# Web UI test checklist

For each failure, note the URL, device, browser, and a screenshot if possible.

## 0. First start

```sh
git pull && docker compose up -d --build
docker compose logs jobalerts | grep "setup code"
curl -s http://<jobalerts-host>:8765/healthz          # expect: ok
```

- [ ] `/` redirects to `/setup`; the setup code and a new password log you in
- [ ] Several wrong passwords cause a delay; `jobalerts set-password` resets the password
- [ ] On a phone, pages fit the screen and the nav scrolls sideways

## A. Jobs

- [ ] Recommended shows fit, step-up and pay index; filters narrow the list
- [ ] Save, Dismiss and Applied work in the list and on a job's page; dismissed jobs leave
      Recommended and the Home Assistant sensor
- [ ] A job's page shows summary, red flags, stories, resume and pay calculation
- [ ] With Settings → Connections → address set, a push opens the job's page

## B. Boards

- [ ] Each watched company is listed with its status; failing boards are first, with the error
- [ ] "Find its job board" finds a known company's board; Add lists it and Next fills in
- [ ] Turn off, Turn on, tier and Remove change `data/companies.yaml` and keep its comments

## C. Filter

- [ ] The counts add up to "new jobs"; each group link lists its jobs with a reason
- [ ] "Score it" on a turned-down job shows a score; nothing is pushed
- [ ] Preview lists jobs that would change; "Try a title" gives a result; neither saves
- [ ] Save, then a new poll uses the new lists; Reset restores the defaults
- [ ] AI only: new jobs show "Waiting for the AI", then a decision within a few minutes

## D. Stories

- [ ] "What the AI sees" updates while typing; TODOs and talk notes are excluded
- [ ] Saving without changing the fields keeps the file's comments
- [ ] "Re-score" shows progress and pushes nothing

## E. Settings

- [ ] Your situation: changes update the pay indexes (`jobalerts pay` agrees)
- [ ] API keys: a saved key shows only "set"; Test works for Anthropic and ntfy; Clear works
- [ ] Profile and resumes: edits save; uploads are listed; Remove moves a file to `.trash`
- [ ] Account: the map link opens the map without a login, and nothing else

## F. Gaps (desktop mode)

- [ ] Stop it for over an hour (or sleep the PC), then start it: one "Catch-up" push, up to 3 job
      pushes, and a "Since you were away" box on the Jobs page
- [ ] A second `jobalerts run` on the same data exits with "already running"

## G. Map

Compare with `docker compose exec jobalerts jobalerts pay 250000 "<city>"`, slider at $250K:

- [ ] The home dot (ringed) has a break-even equal to your salary, renting and buying
- [ ] Albuquerque, Austin, Boston: popup rent and buy indexes match `jobalerts pay` to 0.01
- [ ] Tacoma: a job's index differs slightly from the Seattle dot (dots are metro-wide)
- [ ] Setting the slider to a metro's break-even salary gives an index of 1.00 there
- [ ] Pay index / Break-even and Rent / Buy switch colors, legend and caption
- [ ] The slider recolors from $100K to $400K; it's hidden in break-even view
- [ ] Hover shows name and number; click shows the breakdown
- [ ] Job diamonds and step-up rings are at the right metro, don't hide each other, and link to postings
- [ ] The Jobs checkbox hides and shows job markers
- [ ] *Table & details*: jobs abroad are listed; columns sort; the filter box works; *About* lists sources
- [ ] Reload keeps view, housing, salary and jobs settings
- [ ] Legend: open on desktop, collapsed on a phone
- [ ] State outlines load behind the dots

### Home Assistant

Webpage card from [configuration.md](configuration.md#map).

- [ ] The card shows the map on desktop and in the phone app (home Wi-Fi)
- [ ] `?theme=dark` matches a dark dashboard; without it the page follows the device
- [ ] Two cards with different presets show different views
- [ ] Over https (Nabu Casa or a proxy): note the result; plain http is blocked

### After changes

- [ ] A new top match appears on the map after the next refresh
- [ ] After `docker compose restart` the map returns within a minute
- [ ] `jobalerts map --out data/map.html` writes a file that opens offline

## Known limits

- Only US metros are mapped; jobs abroad are listed. Areas outside metros have no dot.
- Dots use metro-wide figures, without city adjustments or city taxes.
- The viewing browser needs internet access for Leaflet and state outlines.
