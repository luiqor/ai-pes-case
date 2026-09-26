# Introduction

Teams building B2C applications constantly need to decide what to invest in next: adding a new course to a learning app, expanding into another topic, or localizing the product into another language. To make informed decisions about where to invest time and money, they need to understand what their potential audience is interested in and how that interest changes over time.

Wikipedia is one potential source of this insight. Wikimedia publishes article pageview statistics across different language editions, which can be used to study how interest in particular topics changes over time. Of course, interest in a Wikipedia article does not necessarily translate into willingness to pay for a product, but it can still help identify directions worth investigating further.

As part of this task, we propose building a skill for an AI agent that helps B2C product founders decide which topics to develop next and which languages to launch their products in.

## Task

The deliverable should be a standalone skill in the [Agent Skills](https://agentskills.io/specification) format. It should allow an agent to analyze [Wikipedia pageview data](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/reference/page-views.html), generate charts, and produce short, shareable reports, such as a one-page PDF.

Example queries:

* Compare the growth in interest in intermittent fasting in the Polish and Czech Wikipedias over the past two years.
* We are considering adding an astronomy course to an educational app. Is interest in this topic growing in the Ukrainian Wikipedia, and how confident can we be in that trend?
* We are building a language-learning app. Compare interest in learning English across the language editions we selected and prepare a short report on which audiences we should investigate next and why.

These are just examples. Users will have their own topics, languages, and criteria for assessing potential. They may also refine their queries and change their assumptions based on the initial results. The rest is up to you.

Think carefully about how the skill should help the agent evaluate results, validate its conclusions, and efficiently handle follow-up and related queries. Recommendations and reports should be grounded in data, with their assumptions and limitations made clear.

Once the skill can handle the basic queries, explain how you would iteratively evolve it to support more complex research and larger volumes of data.

## Requirements

There is no required tech stack. You may use Python, TypeScript, Go, or any other language.

The skill must include a `SKILL.md` file and its own code that performs a meaningful part of the data-processing work. A Markdown file alone, or instructions telling the agent to write the necessary code from scratch for every request, is not sufficient. Do not include compiled executables; dependencies and environment setup must be reproducible. All custom materials and code must live within the skill directory.

The skill should be convenient and efficient for an agent running on a fast, inexpensive model, such as Claude Haiku 4.5 or a comparable tool-capable model. Even if users ultimately choose more powerful models, this constraint helps validate the quality of the implementation. Test the complete workflow on such a model. You may use inexpensive or [free OpenRouter models](https://openrouter.ai/docs/guides/routing/model-variants/free), taking their capabilities and limitations into account.

Use AI tools during development and be prepared to explain how you evaluated their output.
