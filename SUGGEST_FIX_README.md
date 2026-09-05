# Suggest a Fix — operator notes

## Backend environment (`backend/.env`)

Already present: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`

Required for Suggest a Fix:

```
GEMINI_API_KEY=            # Gemini key. Rotate the one committed in modles.py.
ADMIN_SECRET=              # must match VITE_ADMIN_SECRET in the admin app
```

Required for paid plans (until these are set, `/api/v1/payments/config` reports
`enabled: false` and the upgrade dialog degrades to "not available yet"):

```
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=
RAZORPAY_PLAN_BASIC=       # plan_id for INR 190/month
RAZORPAY_PLAN_PRO=         # plan_id for INR 250/month
```

Optional overrides (defaults shown):

```
SUGGEST_FIX_LIMIT_FREE=2
SUGGEST_FIX_LIMIT_BASIC=6
SUGGEST_FIX_LIMIT_PRO=15
SUGGEST_FIX_MODEL=gemini-2.5-flash
```

## Razorpay webhook

Point it at `POST https://<backend>/api/v1/payments/razorpay/webhook` and
subscribe to the `subscription.*` events. The webhook is the only thing that
grants a plan; the post-payment redirect is UX only.

## Firestore rules

`Perosnal_Portfolio/firestore.rules` blocks the browser from writing `plan`,
`subscription_status` and the usage counters. Without it a user can grant
themselves Pro from the devtools console.

```
firebase deploy --only firestore:rules
```

## Quota

Global per user, resets by UTC calendar date. No reset job — a new date writes
to a new document at `users/{uid}/usage/{YYYY-MM-DD}`.

Analyze costs one credit. Apply costs nothing. A failed LLM call refunds.
