# Work stories

One file per project you'd describe in an interview. The AI scores jobs against the stories and
names the three that fit best; resumes are then used only to pick which version to apply with.
Edit them on the Stories page or in `data/stories/`.

## Format

```markdown
---
id: short-kebab-id              # the file name without .md
title: one line, as you'd say it out loud
employer: Example Corp
dates: 2022–2024
role: lead | contributor | advisor   # your role
skills: [...]                   # what it proves (systems integration, root cause, budget...)
domains: [...]                  # the technology
metrics: [...]                  # numbers exactly as you can defend them
public: yes | partial | no      # can this be shown outside the employer?
sources: [...]                  # papers, reports, figures you may reuse
---
**Situation:** ...
**Task:** ...
**Action:** ... (first person)
**Result:** ...

**Resume bullets** – 1–3 versions
**Talk track** – 30-second and 3–5-minute versions
**Slide idea** – the one figure that makes the point
**Questions it answers** – the interview questions this story is the answer to
```

The AI reads the title, employer, dates, role, skills, domains, metrics, Situation/Task/Action/Result
and resume bullets. It doesn't read talk tracks, slide ideas, questions, `TODO` notes or empty fields.
Don't put confidential employer details in a story marked `public`.
