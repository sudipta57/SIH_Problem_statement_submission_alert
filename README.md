# sih-watch (Vercel)

`GET /api/check` checks SIH26166 on sih.gov.in and emails at 50, 75, 100, ... 500.
Vercel hosts the route, Upstash Redis keeps the state, and cron-job.org triggers it every 15 min
(Vercel Hobby cron is daily-only, so `vercel.json` keeps just one daily run as a backup).

## Setup
1. Push this folder to GitHub, then Vercel -> Add New -> Project -> import the repo (no framework, no build command).
2. Vercel project -> Storage (Marketplace) -> Upstash for Redis -> create a free DB and connect it to this project.
   That adds UPSTASH_REDIS_REST_URL/TOKEN (or KV_REST_API_URL/TOKEN) env vars.
3. Settings -> Environment Variables: add CRON_SECRET, SMTP_USER, SMTP_PASS, MAIL_TO (see .env.example).
4. Redeploy (env vars only apply to new deployments).
5. Verify:
       curl -H "Authorization: Bearer $CRON_SECRET" "https://<app>.vercel.app/api/check?test=1"
       curl -H "Authorization: Bearer $CRON_SECRET" "https://<app>.vercel.app/api/check?dry=1"
   The dry run must show "ok": true and "count": NN.
6. cron-job.org -> Create cronjob:
   URL https://<app>.vercel.app/api/check, every 15 minutes,
   Advanced -> Headers -> Authorization: Bearer <CRON_SECRET>.

## Responses
200 + "ok": false = site fetch failed (counted, you get a mail after 6 in a row).
401 = wrong secret. 500 = config problem (Redis/SMTP env vars).

Reset alerts: delete key `sihwatch:SIH26166` in the Upstash console.
