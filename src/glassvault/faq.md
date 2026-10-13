# Glass Vault Help: frequently asked questions

Glass Vault is a hackathon demo of a conversational banking assistant. It is not a DBS product and is not connected to any real bank. Every customer, account, payee, bill, share price and limit described here belongs to the demo bank, and no real money moves.

Each answer below is under a question heading. "Also asked as" lists other ways customers ask the same thing.

## Is this a real DBS service? Is my money real?
Also asked as: is this DBS, real bank, real money, official app, fake, demo, simulated

No. Glass Vault is a demo built for the DBS track of the Tencent Cloud Hackathon Singapore 2026. It is not a DBS product and is not connected to DBS systems. The customer, accounts, payees, billers, share prices, identity records and the digital token are all simulated, and no real money moves.

## What is this Help panel?
Also asked as: who are you, what is help, are you the assistant, can you see my account, can Help pay for me

Help answers general questions about how this demo bank works, using these FAQs. When Help runs on Tencent ADP, an AI writes each answer from the FAQs, so check anything important; without ADP, Help shows the matching FAQ entry word for word. Help cannot see your accounts, cannot make or change payments, and is never read out in the bank's voice. For anything about your own money, ask the main assistant in the chat.

## What can the assistant do for me?
Also asked as: what can I do, what does the app do, features, what can you help with, supported transactions, capabilities

In the main chat, the assistant can move money between your own accounts, send PayNow to a saved payee, pay a saved bill, buy SGX shares for a cash amount, and answer questions about your balance or recent transactions. One request can combine up to 6 steps, for example: "Move a grand to my joint account, then buy DBS shares with half of it." Every payment needs your passkey approval before anything moves.

## What can't the assistant do?
Also asked as: not supported, overseas transfer, foreign currency, sell shares, crypto, bitcoin, loan, borrow, recurring payment, standing instruction, change settings, unit trusts, funds

It can't add a new payee, pay anyone who isn't already a saved payee, send money overseas or in a foreign currency, sell shares, buy crypto or funds, take a loan, set up scheduled or recurring payments, or change your settings. If you ask for one of these, it should tell you it can't. Either way, the confirmation shows exactly what will be sent, so check it before you approve.

## Can I send money to someone who isn't a saved payee?
Also asked as: new person, phone number, mobile number, account number, pay a stranger, add payee, new payee, PayNow to a number

No. Money can only go to payees already saved on your profile. In this demo they are John Tan Wei Jie, John Lim Kah Seng and Tan Mei Ling (Mum). The assistant never pays a phone number, account number or name that isn't a saved payee, and the demo can't add payees.

## What are my transfer limits?
Also asked as: limit, maximum, how much can I send, transaction limit, PayNow limit, transfer cap, daily limit amount

Your demo profile is on the standard tier: up to S$10,000 per payment and S$20,000 per day for payments to other people (PayNow and bill payments). The basic tier allows S$1,000 per payment and S$2,000 per day, and the premium tier S$50,000 and S$100,000. Transfers between your own accounts don't count toward these limits. Share buys have no per-payment or daily cap, but they count toward extra verification. A request over a limit is refused and nothing is sent.

## When does my daily limit reset?
Also asked as: reset, midnight, tomorrow, daily limit reset time, timezone, new day

At midnight Singapore time (SGT). The daily limit adds up the payments to others you've already made today and the ones in your request. If the total would go over your daily limit, the request is refused and nothing is sent.

## Do transfers between my own accounts count toward my limits?
Also asked as: own account, savings to joint, joint account, move between accounts, internal transfer

No. Moving money between your Savings Account and Joint Account never leaves you, so it doesn't count toward the per-payment or daily limits, the extra-verification threshold, or the check for many payments in a row.

## Why was I asked for extra verification or a token code?
Also asked as: step up, step-up, digital token, 6-digit code, OTP, second factor, extra check, two-factor

Larger or unusual payments are approved twice: with your passkey and with a 6-digit code from your digital token. The code is needed when S$5,000 or more leaves your accounts in one request (PayNow, bills and share buys count; transfers between your own accounts don't), or when a payment is more than 10 times the most you've ever sent that payee, or more than 10 times a bill's amount due. In this demo the token is simulated: tap "Show token (simulated)" to see the code.

## What happens if I enter the wrong token code?
Also asked as: wrong code, incorrect OTP, locked, too many attempts, code not working

You can try again, but after 3 wrong codes that approval is cancelled and nothing is sent. Make the request again to get a new approval. Each code works only for the one approval it was made for.

## Why is my payment to a new payee on hold?
Also asked as: new payee limit, held, on hold, pending, payment pending, cooling-off, cooling off, John Lim, recently added payee, first payment

To protect you from scams, payments to new payees are limited. A payee counts as new for its first 7 days, and until you have paid them at least once more than 7 days ago. If a request would send more than S$1,000 in total to a new payee within 7 days, it's put on hold and nothing is sent. Any payment to a payee added less than 12 hours ago is also held (a cooling-off period). In this demo, John Lim Kah Seng counts as a new payee. A held request isn't processed later on its own: nothing was sent. Try a smaller amount, keeping the total to a new payee within S$1,000 over 7 days, or wait until a newly added payee's 12 hours have passed.

## Why was my request held after several payments in a row?
Also asked as: velocity, too many payments, paused, many transfers, rapid payments, 10 minutes

If you make 5 or more payments to other people within 10 minutes, counting the ones you're asking for, the request is held to protect you. Only PayNow and bill payments count. Nothing was sent; wait a few minutes and try again.

## Why did the assistant ask which John I meant?
Also asked as: two Johns, which John, which payee, ambiguous payee, same name, John Tan

You have two saved payees called John: John Tan Wei Jie and John Lim Kah Seng. When your words fit more than one payee, the assistant asks instead of guessing. It lists each one by legal name and masked PayNow number, from the bank's records.

## Why did it ask me to confirm an unusually large amount?
Also asked as: unusual amount, large payment, just to be sure, outlier, you usually send, bigger than usual

If a PayNow amount is more than 10 times the most you've ever sent that payee, the assistant checks with you first, for example: "Just to be sure: S$6,000.00 to Tan Mei Ling? You usually send about S$500.00." If you confirm, the payment also needs extra verification with your token code.

## Why did it ask whether I said fifteen or fifty?
Also asked as: misheard, voice amount, speech recognition, fifteen fifty, did you say, wrong number heard

Speech recognition can mishear "fifteen" and "fifty". For a spoken PayNow amount, if your payment history makes the other reading much more likely, the assistant asks which you meant. Typed amounts are never questioned this way.

## How do I approve a payment, and what does the passkey do?
Also asked as: approve, Face ID, Touch ID, passkey, biometric, confirm payment, sign, set up Face ID

The assistant drafts the payment, the bank checks it, and you see a confirmation written from the bank's records, which can also be read aloud in the bank's voice. Approve it with your passkey (Face ID or Touch ID). Your device signs that exact confirmation, so the bank only executes what you approved. On a phone or computer with Face ID, Touch ID or Windows Hello, the first confirmation asks you to tap "Set up Face ID", which saves a real passkey for this demo on your device; after that, "Approve with Face ID" signs with it. On a device without one, or if you cancel the setup, the demo uses a clearly labelled simulated approval.

## How long do I have to approve?
Also asked as: expire, expired, time limit, two minutes, approval timeout, quote expired, price expired

2 minutes. After that the approval expires and nothing is sent; just ask again. Share buys also use a price quote that lasts 60 seconds, so if the quote runs out before you approve, ask again for a fresh price.

## Can an approval be used twice?
Also asked as: replay, approve again, reuse approval, double payment, change after approving, edited payment

No. Each approval works once, for that exact request, and only within its 2 minutes. If anything in the payment changed after you approved it, even by one cent, the bank refuses it and nothing is sent.

## How does buying SGX shares work?
Also asked as: buy shares, stocks, invest, DBS shares, OCBC, Singtel, share price, board lot, how many shares, brokerage fee

Tell the assistant how much to invest, for example "Buy S$1,000 of DBS shares". The bank gets a price quote and buys as many whole shares as that amount covers, so the cost never goes over what you said. This demo lists DBS Group Holdings (D05) at S$40.12, OCBC Bank (O39) at S$16.85 and Singtel (Z74) at S$3.91. These are fixed demo prices, and you can buy any whole number of shares rather than real board lots of 100. The demo doesn't model fees or selling shares. Buying shares needs the demo's investment-eligibility check, which your demo profile has passed.

## Can I say "half of it" or "a quarter of my savings"?
Also asked as: half of it, fraction, percent, quarter, third, part of my savings, relative amount

Yes. "Half of it" means half of an earlier step in the same request, for example: "Move a grand to my joint account, then buy DBS shares with half of it." "A quarter of my savings" means a quarter of that account's balance when the step runs. Fractions are rounded down to the cent and are always worked out by the bank's code, never by the AI.

## Which account does a payment come from?
Also asked as: source account, from which account, default account, pay from joint, pay from savings

Payments come from your Savings Account unless you say otherwise. If one step tops up an account and a share buy, or a step spending part of that transfer, follows without saying where the money comes from, it's paid from the account you just topped up. If the draft doesn't match that, the assistant asks you which account to use.

## Can I pay my bills without knowing the amount?
Also asked as: pay bill, bill amount, amount due, SP Group, Singtel bill, pay all my bills, utilities

Yes. If you don't say an amount, a bill is paid at its full amount due. "Pay both my bills" or "pay all my bills" pays every saved biller in full. In this demo your billers are SP Group and Singtel.

## Why does the confirmation read large amounts twice?
Also asked as: read twice, digit by digit, repeated amount, bank voice, spoken confirmation

Amounts of S$1,000 or more, and amounts flagged as unusual, are read twice: first in words, then digit by digit, because "fifteen" and "fifty", or "fifty" and "fifty thousand", are easy to mishear. The words you hear are part of what your passkey signs.

## Why does the confirmation show a legal name instead of my nickname?
Also asked as: Mum, nickname, Tan Mei Ling, legal name, masked number, bill reference missing

The confirmation always uses the bank's records: a payee's legal name and masked PayNow number, never a nickname. Nicknames and bill references are free text the bank can't vouch for, so they're never shown on the confirmation.

## Why did the assistant refuse a message I pasted?
Also asked as: refused, pasted message, copied text, scam, instructions, won't act, someone told me to

Scammers often send people a script to copy and paste. If your message contains orders aimed at the AI itself, such as telling it to forget its rules or pretending to be a system message, the assistant won't act on it. Tell it what you want in your own words. General safety advice: a bank will never ask you to move money to a "safe account" or to share your token code with anyone.

## Why did it say "Nothing has been sent"?
Also asked as: nothing sent, stopped, frozen, failed, partly sent, did my money go, all or nothing

Every request is all-or-nothing: each step is checked before any money moves, so if one step can't go ahead, none of them do. No money moves when a request is stopped because a safety check didn't match what you asked, held (for example by a new-payee limit), refused (such as over a limit or short of money), cancelled, or left to expire. The message says which one happened.

## Can I check my balance or recent transactions?
Also asked as: balance, how much money, statement, transactions, history, withheld description

Yes. Ask the main assistant, for example "What's my balance?" or "Show my last 5 transactions". It answers from the bank's records and needs no approval. A statement line whose description reads like instructions is withheld. Help itself can't see your accounts.

## Does the AI decide whether my payment is safe?
Also asked as: how does it work, is it safe, can the AI steal, security, trust, what stops the AI

No. The AI only drafts a request. Code checks that every amount and payee came from your own words and does all the maths. A second AI reads your request independently, and any disagreement stops it. Fixed risk rules decide holds and extra checks, and only your passkey can approve. The bank's gateway then executes exactly what you signed, and every approval and payment is written to a tamper-evident log.

## Which languages and phrasings does it understand?
Also asked as: language, Singlish, a grand, 5k, bucks, Mandarin, Chinese, Malay

English, including everyday Singapore phrasing such as "a grand", "5k" or "fifty bucks". Amounts in Mandarin or Malay numerals aren't supported.

## What doesn't this demo cover?
Also asked as: password, reset password, login, lost phone, lose my phone, stolen phone, card, debit card, credit card, interest rate, fees, charges, other bank, OCBC account, UOB, overseas bank, branch, hotline, customer service, overdraft, cheque

This demo models one customer's payments only. It has no logins or passwords, cards, interest, fees, cheques, branches or hotline, and it can't send money to other banks. For questions about a real bank account, contact your bank through its official channels.
