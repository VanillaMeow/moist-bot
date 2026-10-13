# Bound counter storage and the number of references retained for burst cleanup
MAX_BUCKETS_PER_GUILD = 4096
MAX_BURST_MESSAGES = 100

# Spam rows share the tracker table without sharing other services' state
STATE_NAMESPACE = 'spam'
CONTENT_EXCERPT_LIMIT = 500

# Eligibility uses member metadata and does not flag member joins
NEW_ACCOUNT_DAYS = 90
NEW_MEMBERSHIP_DAYS = 7
