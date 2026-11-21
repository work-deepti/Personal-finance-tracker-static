# Investment Tracker for Vercel

This project runs the full tracker with private, per-account holdings, equity transactions, and history. It starts with an empty portfolio. The optional sample holdings are added from Settings. The standalone demo and Windows ZIP are separate packages.

## Set up Supabase

1. Create a Supabase project. In **SQL Editor**, run `schema.sql`. It creates private tables for portfolios and sign-in sessions.
2. In **Authentication → Users**, create and confirm an email/password account. Disable new user sign-ups in your project's Auth settings for a private tracker. Create additional accounts in the Auth dashboard if needed. Password resets can be handled from the Supabase dashboard.
3. Copy your project URL, publishable key, and the **Transaction pooler** connection string from **Connect**. Replace the database password placeholder and URL-encode special characters in that password.

## Deploy to Vercel

1. Import this repository into Vercel. Let Vercel detect Flask.
2. Add these environment variables for **Production** in Vercel's project settings.

   | Name | Value |
   | --- | --- |
   | `APP_ORIGIN` | Exact production URL, including `https://`, with no path, such as `https://your-tracker.vercel.app` |
   | `SUPABASE_URL` | Supabase project URL |
   | `SUPABASE_PUBLISHABLE_KEY` | Supabase publishable key |
   | `DATABASE_URL` | Supabase transaction pooler connection string |

3. Deploy or redeploy after setting the variables. Open the exact URL configured in `APP_ORIGIN` and sign in.
4. Use Settings to load sample data. Add a holding or record a trade, reload, and sign out and back in to confirm it persists. If you create a second test account, verify that it starts with an empty portfolio.

For a custom domain, update `APP_ORIGIN` to match and redeploy. Keep the database connection string in Vercel's environment settings. If you deploy a preview, use a separate database project for testing and set its exact origin.

## Existing Windows data

The online app starts with a fresh database. Use **Export CSV** in the Windows app and **Import from file** online to transfer holding summaries. CSV does not transfer detailed equity trades or historical snapshots. Keep your Windows data and backups.

Online imports accept files up to 3 MiB. Sessions expire after 12 hours. The app uses Supabase email/password authentication and stores a hash of each web session token in Postgres. Holdings and history are stored by account. No registration page is exposed.
