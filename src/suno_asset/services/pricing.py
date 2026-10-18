"""Nominal estimates inherited from the M7 baseline; never authorization to bill."""
GROQ_PRICES = {'openai/gpt-oss-20b': (0.075, 0.30), 'openai/gpt-oss-120b': (0.15, 0.60)}
PRICES = dict(GROQ_PRICES)
PRICE_SOURCE = 'https://console.groq.com/docs/models'
# Standard text rates per million tokens, checked 2026-10-17. These are
# nominal paid-tier comparisons, never a claim that the account is free.
# Google publishes higher rates starting 2027-01-01; admission fails closed
# after this snapshot expires until the prices are revalidated.
PRICES['gemini-3.8-flash'] = (0.75, 3.75)
PRICES['gemini-3.6-flash'] = (1.50, 7.50)
PRICES['gemini-3.5-flash-lite'] = (0.30, 2.50)
GEMINI_PRICE_SOURCE = 'https://ai.google.dev/gemini-api/docs/pricing'
GEMINI_PRICE_VALID_UNTIL = '2026-12-31'
