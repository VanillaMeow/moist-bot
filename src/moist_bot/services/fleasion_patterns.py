import re

HELP_SOMEONE = r'(?:someone|somone|somebody|anyone|anybody|some\s*1|any\s*1)'
HELP_GREETING = (
    r'(?:(?:yo+|hey|hi|hello|hiya|sup|ayo|ey|oi|guys|chat|'
    r'y[\x27\u2019]?all|everyone|anybody|folks|people|gang|team|'
    r'bro|bros|bruh|bruv|boi|dude|man|mate|pls|plz|please|so|also)\W+)*'
)
HELP_APP_NAME = r'fleasi?on\b'
HELP_APP_ACTION = r'(?:work(?:s|ing)?|run(?:s|ning)?|open(?:s|ing)?|launch(?:es|ing)?|load(?:s|ing)?)\b'
HELP_APP_STATUS = (
    r'(?:\s+on\s+\w+)?\s+(?:(?:still|not|even|actually|currently|just)\s+)*'
    rf'(?:{HELP_APP_ACTION}|crash(?:es|ing)?\b|broken\b|down\b|offline\b|online\b)'
)
HELP_REQUEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Ask about the app's status without requiring a particular operating system
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}(?:why\s+)?'
            r'(?:is|isn[\x27\u2019]?t|does|doesn[\x27\u2019]?t|did|has|will|'
            r'won[\x27\u2019]?t|can|can[\x27\u2019]?t)\s+(?:my\s+)?'
            rf'{HELP_APP_NAME}{HELP_APP_STATUS}'
        ),
        (
            rf'\b{HELP_SOMEONE}\s+know\s+(?:if|why|whether)\s+'
            rf'(?:is\s+|does\s+)?{HELP_APP_NAME}'
            rf'(?:\s+is|\s+does)?{HELP_APP_STATUS}'
        ),
        # Failure reports often omit question words and punctuation entirely
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}(?:why\s+)?(?:my\s+)?{HELP_APP_NAME}'
            r'\s+(?:(?:(?:is\s+)?(?:still\s+)?not|isn[\x27\u2019]?t|'
            r'doesn[\x27\u2019]?t|won[\x27\u2019]?t|stopped)'
            rf'\s+(?:(?:even|actually)\s+)?{HELP_APP_ACTION}|'
            r'(?:keeps|is)\s+crashing\b)'
        ),
        # Require questions to start a message or sentence, after optional greetings
        (
            rf'(?:^|[.!?]\s+){HELP_GREETING}'
            r'(?:hel+p+\b|(?:need|want)\s+(?:(?:some|sum)\s+)?hel+p+\b|'
            r'how\s+(?:to|get|do|can|should)\b|what\s+(?:do|should|can)\b|'
            r'what\s+does\s+(?:this|that|it)\s+mean\b|'
            r'why\s+(?:is|are|does|do)\s+(?:my|the|this|that|it)\b)'
        ),
        # Keep explicit requests separate from offers, thanks and mentions of help
        (
            r'\b(?:i|we)\s+(?:(?:just|js|really|rlly)\s+)*'
            r'(?:need|want)\s+(?:(?:some|sum)\s+)?hel+p+\b'
        ),
        (
            rf'\b{HELP_SOMEONE}\s+'
            r'(?:(?:here|can|could|just|js|pls|plz|please)\s+)*hel+p+\b'
        ),
        r'\b(?:pls|plz|please)\s+hel+p+\b',
        r'\bhel+p+\s+me+\b',
        r'\bwho\s+(?:can|could)\s+hel+p+\b',
        r'\b(?:can|could)\s+i\s+(?:get|have)\s+(?:(?:some|sum)\s+)?hel+p+\b',
        r'\bany\s+help\s+(?:would|will)\s+be\s+appreciated\b',
        # Requests can include a call or other context before the actual help verb
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|you|u)\b'
            r'[^.!?]{0,100}\b(?:hel+p+|show|teach|explain)\b'
        ),
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|you|u)\b'
            r'[^.!?]{0,40}\b(?:tell|dm)\s+me\b[^.!?]{0,40}\bhow\s+to\b'
        ),
        # Indirect questions and admissions of uncertainty still ask for instructions
        (
            rf'\b{HELP_SOMEONE}\s+(?:of\s+you\s+guys\s+)?'
            r'know\s+how\s+to\b'
        ),
        r'\bdo\s+(?:(?:you|u)\s+know|yk)\s+how\s+to\b',
        r'\b(?:idk|i\s+don[\x27\u2019]?t\s+know)\b[^.!?]{0,60}\bhow\s+to\b',
        (
            r'\bis\s+there\s+(?:a|an)\s+(?:vid|video|tutorial|tuto)\b'
            r'[^.!?]{0,60}\bhow\s+to\b'
        ),
        r'\b(?:can|could|may)\s+i\s+ask\s+for\s+help\b',
        r'\bis\s+it\s+(?:ok|okay)\s+if\s+i\s+ask\s+for\s+help\b',
    )
)


CONFIG_NAME = r'\b(?:cnfg|cfg|config)s?\b'
CONFIG_REQUEST_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        # Ask whether someone has a config, including common chat abbreviations
        (
            rf'\b(?:{HELP_SOMEONE}|anb|you|u)\s+(?:have|has|got|know)\s+'
            rf'(?!why\b|how\b|if\b|whether\b)[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        # Ask another user to share, send or create a config
        (
            rf'\b(?:can|could|would|will)\s+(?:{HELP_SOMEONE}|anb|you|u)\s+'
            rf'(?:share|send|give|link|make)\b[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        rf'\b(?:send|give|link|make)\s+me\b[^.!?]{{0,160}}{CONFIG_NAME}',
        rf'\b(?:looking|searching)\s+for\b[^.!?]{{0,160}}{CONFIG_NAME}',
        (
            r'\b(?:i|we)\s+(?:need|want)\s+(?!(?:(?:some|sum)\s+)?help\b|to\b)'
            rf'[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        rf'\b(?:can|could)\s+i\s+(?:get|have)\b[^.!?]{{0,160}}{CONFIG_NAME}',
        (
            r'\b(?:is|are)\s+there\s+'
            r'(?!(?:(?:a|an|any|some)\s+)?(?:reason|problem|issue|way|fix)\b)'
            rf'[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
        (
            r'\bwhere\s+(?:(?:can|do)\s+(?:i|we)\s+|to\s+)?'
            rf'(?:find|get|download)\b[^.!?]{{0,160}}{CONFIG_NAME}'
        ),
    )
)
