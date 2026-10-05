# 🕵️ Fake Sale Detector

**Is that "WAS £449, NOW £134" actually real?** Paste a UK product link. The detector checks what the price *really* was over the past year, using the Wayback Machine. It also checks whether other shops are cheaper right now, then gives you a verdict.

🟢 Genuine price drop · 🟡 Not really a deal / cheaper elsewhere · 🔴 Fake sale ("was" price never existed)

It also flags **pre-sale price hikes**, where a shop quietly raises the price a few weeks before a sale so the discount looks bigger.

## How TinyFish is used

| Step | TinyFish endpoint | What it does |
|---|---|---|
| 1 | **Fetch** | Reads the live product page to get the name and today's price |
| 2 | **Fetch** | Queries the Wayback Machine CDX API for every archived copy of that exact URL |
| 3 | **Fetch** | Reads up to 6 old snapshots in parallel to get the real historical prices |
| 4 | **Search** | Finds the same product at other UK retailers |
| 5 | **Agent** | Reads the live price on the top competitor page (and is the fallback when a page won't parse) |
| 6 | **Monitor** | Re-checks the product daily and alerts on any price change |

## Run it

```bash
pip install requests
export TINYFISH_API_KEY=your_key   # https://agent.tinyfish.ai/api-keys
python fakesale.py "https://www.argos.co.uk/product/3284627"
# optional: tell it the "was" price the shop is claiming
python fakesale.py "https://www.argos.co.uk/product/3284627" --was 449.95
```

The command prints the verdict and opens `report.html`, which contains a verdict card, a price-history chart, and a competitor table.

## Example result (real data, 5 Oct 2026)

**Bose QuietComfort Ultra headphones at Argos, £134.99. Verdict: 🟢 Genuine price drop**

- The price is 61% below its typical archived price of £349.95, and it's the lowest price on record.
- ⚠️ Argos raised the price from £349.95 to £449.95 in October 2025, right before Black Friday.
- No other retailer we checked was cheaper. The next best was Amazon at £289.00, a live price read by the TinyFish Agent.

**Henry XL Plus vacuum at Argos, "Was £200, now £139.99". Verdict: 🔴 Fake sale**

```
python fakesale.py "https://www.argos.co.uk/product/8801452" --was 200
```
- Argos claims "Save £60", but none of the 5 archived snapshots from the past year (May 2025 – Apr 2026) show £200. The highest is £180.
- It was £140 in May 2025 and April 2026, so the "£60 saving" is really 1p.
- The price went from £140 to £180 in September 2025, which inflated the reference price.

Reports: [`report-bose.html`](report-bose.html) (Bose) · [`report-henry.html`](report-henry.html) (Henry)

A TinyFish Monitor checks the Bose page every morning at 9am UK time.

*Note: the Wayback Machine stores snapshots, not every price change, so a verdict means "never seen in the archive", not proof the price never existed.*
