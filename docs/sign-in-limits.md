# Sign-in limits

Wrong passwords are slowed down, and nobody is locked out for good. This page says what the rule is. The code is `app/auth_limits.py`.

## What is counted

Every attempt at the password step, the second-factor step, and a password asked for again from someone already signed in (to change a password or a factor) is counted before it is judged. A right answer takes its own count back. Three counts are kept:

| Count | Allowed before waiting starts | What it stops |
| --- | --- | --- |
| One account from one address | 4 | Someone guessing one account |
| One address, any account | 19 | One machine trying many accounts |
| One account, any address ("account-wide") | 19 | Guesses at one account spread over many addresses |

Past the allowance each further attempt waits twice as long as the last, from 2 seconds up to 5 minutes. While a wait is running the attempt is refused, **even with the right password** (otherwise the wait would not slow guessing), and says how long is left. Failures are forgotten after 15 quiet minutes, and a completed sign-in clears the account's counts.

An address already being slowed for an account adds nothing more to that account's account-wide count. One outside address cannot make everyone wait.

IPv6 addresses are counted by their /64: one host has a whole /64 of addresses to use.

## Who the account-wide wait leaves alone

A few outside addresses, guessing in turn, can keep the account-wide wait running for as long as they like. So that this does not keep the account's owner out, the account-wide wait **never applies to**:

- **anyone on the local network** (what counts as local is in [remote-access.md](remote-access.md), "Where local ends"; people on a VPN tunnel are not local unless you list the tunnel);
- **a browser in which a sign-in to that account has been completed before.** The browser is given a cookie (`nvr_known_browser`) when a sign-in completes. It is not a session and opens nothing; the password and the second factor are still asked for. It is HttpOnly and SameSite=Strict, is tied to the one account, lasts 180 days, and stops counting when the account's password changes or the account is disabled.

Those callers are limited by their own counts only (the first two rows of the table), and their wrong attempts are not added to the account-wide count.

**From any other outside address, a right password still waits** while the account-wide wait is running. That is deliberate: letting a right password through from anywhere would let guessing spread over many addresses go unslowed. In practice: sign in from a browser you have used before, or from the site.

## When someone is guessing

When the account-wide wait starts for an account that exists, the NVR raises an incident naming the account ("Wrong passwords for the account … are arriving from several addresses"). It recovers when that account is signed in to, or once the guessing has been quiet for 15 minutes and anyone next signs in. The security log holds each `auth_throttled` entry with the address.

## What is not limited by this

Microsoft Entra sign-in (the tenant's own limits apply), API tokens, and video-wall sessions.
