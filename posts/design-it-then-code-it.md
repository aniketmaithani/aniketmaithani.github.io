---
title: "Design It, Then Code It: What Shipping LLM Products Taught Me About Architecture and PII"
date: 2026-06-01
author: Aniket Maithani
tags: [llm, architecture, security, pii, ai, fintech]
description: "Most LLM products ship to production without anyone drawing a single diagram. After fifteen years in fintech, here is why I design the data flow on paper first — and how LLMs broke my old assumptions about protecting personal data."
reading_time: 7
status: published
---

Most LLM products I have seen go to production without anyone drawing a single diagram. The demo worked on a laptop, the stakeholders clapped, and the team started writing the real thing the next morning. I understand the pull. I have felt it. You wire up an API key, paste a prompt, and something that looks like magic comes back in an afternoon. After fifteen years of building fintech systems where nothing useful happens in an afternoon, that speed is intoxicating.

But the demo is not the system. And with LLMs, the distance between the two is wider than anything I dealt with shipping payment gateways or digital gold platforms. The demo hides the parts that actually decide whether your product survives a load spike, a cost review, or a security audit. The only way I have found to see those parts before they bite is to design the system on paper before I write code. Not a fancy design doc nobody reads. A real one. Boxes, arrows, and an honest accounting of where data goes.

## The diagram you skip is the incident you get

When I started building a media intelligence pipeline at M37, the first version was a script. It pulled search results, scraped the pages, fed the text to a model, and rendered a report. It ran. It was also a trap. There was no diagram, so nobody could see that a single slow page could hang the whole run, that the scraping step had no backpressure, and that every piece of content passed through the model in plaintext with no record of what came from where.

The second time around I drew it first. A simple sequence diagram, the kind you can sketch in ten minutes. Source query goes to a search provider. Results go to a fetcher. Fetcher hands raw HTML to an extractor. Extractor hands clean text to a cheap model for a first pass, then a stronger model for the synthesis, then a renderer. The moment that diagram existed, the problems were obvious. Every arrow that crossed from "my servers" to "someone else's API" was a place I was losing control of data. Every arrow that fanned out was a place I needed a queue and a worker pool, not a for loop.

This is the part people miss about architecture work. It is not about drawing pretty boxes for a slide. It is about making invisible decisions visible early, while they are still cheap to change. A trust boundary you can see is a trust boundary you can defend. One you discover in production is an incident.

The same lesson showed up in the boring infrastructure layer. Running the backend on AWS in the Mumbai region on Graviton instances was a deliberate choice, made on paper, for cost and data residency reasons. But the real lessons were the ones the diagram forced me to think through before I hit them: the nginx timeouts that kill long model calls if you do not raise the upstream limits, the WebSocket upstream config you need for streaming, the Celery workers that have to run on gevent because the work is IO bound on external APIs and not CPU bound. None of that is hard once you know it. All of it is brutal to discover at 2am because you skipped the design and the architecture revealed itself through outages instead.

## Code is the cheap part now

Here is the uncomfortable truth that the LLM era has made plain. Writing code is no longer the bottleneck. I can produce a working implementation faster than ever, and so can the people I work with. What does not get faster is knowing what to build. The judgment about where the queues go, which model handles which step, what happens when an external API is down, how you cap cost, what you log and what you must never log. That judgment lives in the design, not the code.

So I have flipped my own habit. I spend more time on the architecture than I used to, and less time on the first implementation, because I trust myself to write the code quickly once the shape is right. When I design first, the code becomes almost a transcription of the diagram. The fetcher is a function. The queue is a queue. The trust boundary is a redaction step. The cost cap is a real component because I decided on paper that it had to exist, not a thing I bolted on after the first invoice scared me.

That cost point is not a side note. I built a small monitoring job that watches model API spend and emails me before it gets out of hand, with proper timestamps and deduplication so it does not spam me. That component exists because the architecture diagram had a box that said "this talks to a paid API in a loop" and I asked the obvious question: what stops this from costing a fortune. You only ask that question if you drew the loop first.

## LLMs broke my assumptions about PII

Now the part I think we are all collectively underrating. LLMs change the shape of your data security problem, and most teams are applying old instincts to a new perimeter.

In fintech I learned to protect data at rest and data in transit. You encrypt the database. You use a hardware security module or a key management service so the keys are not sitting next to the data. You get your PCI DSS and your ISO 27001 in order. I have done zero knowledge encryption work where the whole point is that even the operator cannot read the contents. These controls are mature and they work.

None of them protect data that you put inside a prompt.

When you send text to a model API, that text leaves your perimeter in plaintext as far as your encryption is concerned. Your CloudHSM does not help you. Your encrypted volume does not help you. The data is now in the prompt, traveling to a provider, possibly logged on their side, possibly logged on yours for debugging, and sitting in your application memory in the clear. The protections you spent years building all sat at rest and in transit. The prompt is a third state that your old threat model did not have a name for.

This matters even more in any product that ingests outside content. A competitive monitoring pipeline scrapes the open web and feeds it to a model. You think you are processing public articles, but public pages contain names, contact details, and personal information all the time. The moment that text enters a prompt, you are processing personal data, whether you intended to or not. If you also keep prompt logs to debug your pipeline, congratulations, you have built a personal data lake without filing the paperwork.

## What I actually do about it

I do not have a silver bullet, but I have a set of habits that come straight from designing the data flow before writing the code.

First, redaction happens before the prompt, not after the response. If a step does not need a person's name, email, or phone number to do its job, I strip or tokenize it on the way in. The model can reason about "Person A" and "Company B" perfectly well for most analysis tasks. You map the real values back in at the end, inside your own perimeter, if you need them at all.

Second, I treat prompt logs as the most sensitive logs in the system. The instinct to log every full prompt and response for debugging is understandable and dangerous. Either you scrub them, you keep them for a short fixed window, or you accept that you have built a regulated data store and you secure it like one. There is no fourth option where you log everything forever and it is fine.

Third, data residency has to follow the data, not the database. It is meaningless to host your servers in the Mumbai region for residency reasons and then ship every prompt to a model endpoint in another country. Where your model calls actually go is part of your data map. If a provider offers a zero data retention arrangement or a region you can pin, that is an architectural decision, and it belongs in the diagram next to everything else.

Fourth, encryption and tokenization are not the same tool and the LLM context is where the difference bites. Encryption protects data you store. Tokenization lets data move through a system that should never see the real value. For prompts, tokenization is usually the right instinct, because the model is exactly the component that should not see the real value.

## The two disciplines are one discipline

I used to think of architecture and security as separate concerns, handled by separate documents at separate points in a project. Building LLM products cured me of that. In these systems the architecture decisions are the security decisions. Where a trust boundary sits, what crosses it, what gets logged, which model runs in which region. You cannot answer a single one of those questions from the code. You can only answer them from the design.

So I design first. Not because it is virtuous, and not because some methodology told me to. I do it because the design is the only place where the cost, the failure modes, and the personal data all become visible at the same time, while they are still cheap to fix. The code is the easy part. It was always going to be the easy part. The hard part is knowing what you are building before you build it, and being honest about where the data goes once you do.

Draw the diagram. Especially the arrow that leaves your perimeter. That is the one that will keep you up at night if you pretend it is not there.
