# Investment Tracker: browser-only version

This folder is a separate, self-contained version of the tracker. It needs no database, no sign-in and no environment variables: the whole app is `index.html`, and it keeps your investments in the browser you use it from.

It is independent of the Flask + Supabase app in the repository root. Nothing here changes that app.

| File | What it is |
|---|---|
| `index.html` | The app. |
| `api/prices.js` | A small price service that runs on Vercel, so stock and ETF prices work. The page finds it by itself. |
| `worker.js` | The same price service for Cloudflare Workers. Only needed if you host the page somewhere other than Vercel. |
| `import_template.xlsx` | Spreadsheet for adding investments in bulk. |

## Deploy on Vercel

1. In Vercel, add a new project and import this repository.
2. Set **Root Directory** to `static`. This is the step that matters: without it Vercel builds the Flask app in the repository root instead.
3. Leave the framework preset as "Other", with no build command and no environment variables.
4. Deploy, then open the address Vercel gives you.
5. In the tracker, open Settings (the gear icon) and press "Save and test" with the address box empty. It should report that the site's own price service works and show the Nifty 50 level.

If you already have a Vercel project for the Flask app, create a second project for this folder so the two do not replace each other.

## Why the price service exists

Mutual fund and crypto prices are fetched straight from the browser. Stock and ETF prices come from Yahoo Finance, which refuses requests sent directly from a web page, so the page asks `api/prices.js` and that asks Yahoo. It forwards only a short allow-list of price addresses and only ever sees ticker symbols such as `RELIANCE.NS`, never your holdings or amounts.

## Your data

- Investments are saved in one browser on one device. They do not sync, and clearing browser data erases them.
- Use Settings, "Download backup" now and then, and keep the file somewhere safe. "Restore from backup" loads it on another device or browser.
- "Restore from backup" also accepts a `holdings.json` file from the desktop version.

## Good to know

- Crypto prices: CoinGecko allows only a few requests a minute without a key. The app asks for all coins in one request and reuses prices for 10 minutes. If they still fail often, paste a free CoinGecko "demo" key in Settings.
- When a price source is down, the app shows the last price it fetched and says so under the pie chart.
- Excel import needs a browser from 2023 or later. On older ones, save the sheet as CSV and import that.
- Prices are delayed quotes from free public sources. Do not rely on them for trading.
- Live price sources could not be reached from the environment this was built in, so the price paths were tested against simulated data. Check real prices on first use.
