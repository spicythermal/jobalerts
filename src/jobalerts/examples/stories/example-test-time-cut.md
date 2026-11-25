---
id: example-test-time-cut
title: Cut a production test from 40 to 12 minutes without losing coverage
employer: Example Corp
dates: 2021–2022
role: lead
skills: [test engineering, data analysis, cross-team coordination]
domains: [production test, automation]
metrics: ["test time 40 → 12 min", "no change in field return rate over 12 months"]
public: yes
sources: []
---
**Situation:** The end-of-line test was the factory bottleneck; adding a second station would have cost a year of budget.

**Task:** Make the existing station fast enough without letting more bad units through.

**Action:** I pulled two years of test logs, found which steps had never failed on their own, merged them with the steps that always failed together, and ran the shortened sequence side by side with the old one for six weeks.

**Result:** Test time fell from 40 to 12 minutes, the second station wasn't needed, and field returns stayed flat for the following year.

**Resume bullets**
- Cut end-of-line test time 70% (40 → 12 min) using failure-history analysis, avoiding a second test station.

**Questions it answers**
- Tell me about a time you used data to change a process.
