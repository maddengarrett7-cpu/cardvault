# Product Marketing Context

*Last updated: 2026-06-09*

## Product Overview
**One-liner:** SlabScan is a mobile-first card scanner that instantly identifies sports cards and Pokémon/TCG cards — graded or raw — and pulls live eBay market values.
**What it does:** Point your phone camera at any graded sports card slab and SlabScan uses AI to read the player, year, brand, and grade — then fetches real eBay sold prices (avg/high/low + recent sales) so you know exactly what it's worth. Syncs your collection to Google Sheets automatically.
**Product category:** Sports card collection management / card valuation tool
**Product type:** PWA (installable web app), SaaS
**Business model:** Freemium subscription — Free tier (10 scans/day), Pro at $7.99/mo or $59/yr (unlimited scans + full eBay data)

## Target Audience
**Target users:** Sports card collectors, hobbyists, flippers, and dealers — anyone who buys, sells, or manages a graded card collection
**Decision-makers:** Individual collectors (B2C), small card shop owners
**Primary use case:** Instantly know the market value of a graded card without manually searching eBay
**Jobs to be done:**
- "Help me price cards quickly so I don't leave money on the table at shows or when selling"
- "Help me track my collection's total value without a spreadsheet nightmare"
- "Help me decide whether to buy a card at a show or flea market in real time"
**Use cases:**
- Scanning cards at a card show to check values before buying
- Building a Google Sheets inventory of a full collection
- Quickly pricing out slabs to list on eBay

## Personas
| Persona | Cares about | Challenge | Value we promise |
|---------|-------------|-----------|------------------|
| The Flipper | Fast, accurate pricing | Manually searching eBay for every card takes too long | Instant eBay sold data with one scan |
| The Collector | Knowing total collection value | Spreadsheets are tedious; values go stale | Auto-syncing Google Sheet that stays current |
| The Dealer / Shop Owner | Pricing inventory at scale | Too many cards, not enough time | Batch scanning with session totals |

## Problems & Pain Points
**Core problem:** Graded card values change constantly and manually looking up every card on eBay is slow and tedious — especially at a card show or when pricing a large collection.
**Why alternatives fall short:**
- eBay search: Manual, slow, hard to filter sold comps correctly on mobile
- Card Ladder: Great data but requires manual entry of player/year/grade
- Spreadsheets: No live pricing, no camera integration
**What it costs them:** Missing deals at shows, underpricing cards when selling, hours wasted on manual lookups
**Emotional tension:** Anxiety about overpaying or underselling; frustration from slow manual research

## Competitive Landscape
**Direct:** Card Ladder — requires manual search entry, no camera scan
**Direct:** CollX — photo scan app but focused on raw cards, not graded slabs
**Secondary:** eBay app — manual search, no AI identification, cluttered mobile UX
**Indirect:** Google Sheets + manual pricing — no automation, values go stale instantly

## Differentiation
**Key differentiators:**
- Camera scan → AI reads slab label automatically (no manual typing)
- Live eBay sold comps (avg/high/low) pulled instantly
- Google Sheets sync with smart column detection
- PWA — works on any phone, no app store needed
- Built specifically for graded slabs (PSA, BGS, SGC, etc.)
**How we do it differently:** Combine AI vision (Gemini) + eBay scraping in a single tap — no manual entry required
**Why that's better:** Faster decisions, less friction, works hands-free at a show
**Why customers choose us:** Speed + accuracy — know what a card is worth before the seller finishes their pitch

## Objections
| Objection | Response |
|-----------|----------|
| "Is the eBay data accurate?" | We pull real sold listings — same data you'd see manually, just automated |
| "Does it work on all graded cards?" | Works on PSA, BGS, SGC, and other major graders — AI reads the label |
| "Why pay $9.99/mo?" | One good deal at a show more than pays for it; unlimited scans vs. 10/day free |

**Anti-persona:** Collectors who don't care about market value and just want a digital catalog

## Switching Dynamics
**Push:** Wasting time on manual eBay lookups; missing deals at shows because pricing is too slow
**Pull:** Instant scan-to-value in one tap; no more manual searching
**Habit:** "I've always just searched eBay myself" — familiar but slow
**Anxiety:** "What if the AI gets the card wrong?" — mitigated by showing full card details for review

## Customer Language
**How they describe the problem:**
- "I spend way too long looking up every card"
- "I don't want to overpay at a show"
- "I have no idea what my collection is actually worth"
**How they describe the solution:**
- "Just scan it and know what it's worth"
- "Like a price gun for sports cards"
**Words to use:** scan, instant, graded, slab, market value, eBay comps, collection, flip, PSA, BGS
**Words to avoid:** "AI-powered" (too generic), "platform" (too corporate)
**Glossary:**
| Term | Meaning |
|------|---------|
| Slab | A graded card in a plastic case from PSA, BGS, SGC, etc. |
| Comps | Comparable sold listings on eBay |
| Pop | Population report — how many cards graded at a given grade |

## Brand Voice
**Tone:** Direct, no-BS, built for serious collectors
**Style:** Casual but confident — like a knowledgeable collector friend
**Personality:** Fast, sharp, trustworthy, practical

## Proof Points
**Metrics:** Pulls avg/high/low from recent eBay sold listings; session total value tracker
**Value themes:**
| Theme | Proof |
|-------|-------|
| Speed | One tap to full eBay comps |
| Accuracy | AI reads player, year, brand, grade from slab label |
| Convenience | PWA — no app store, works on any phone |

## Goals
**Business goal:** Grow Pro subscriber base ($9.99/mo recurring)
**Conversion action:** Upgrade to Pro after hitting 10 scan/day free limit
**Current metrics:** Free (10 scans/day) vs Pro (unlimited) — Stripe subscription active
