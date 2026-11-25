# Work stories

One file per story: a piece of work you'd tell in an interview. When you have stories, the AI
judges each job against them (and names the three that fit best); resumes then only pick which
version to apply with. Write them on the Stories page or as files in `data/stories/`.

## Format

```markdown
---
id: short-kebab-id              # the file name without .md
title: one line, as you'd say it out loud
employer: Example Corp
dates: 2022–2024
role: lead | contributor | advisor   # what YOU did; keeps generated text honest
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

What the AI sees: the title, employer, dates, role, skills, domains, metrics and the
Situation/Task/Action/Result and resume bullets. Talk tracks, slide ideas and questions are for
you; `TODO` notes and empty fields are left out. Mind `public`: don't put your employer's
confidential details in a story you might share.
