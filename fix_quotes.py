"""Replace only the garbled emoji in Telegram message strings with clean ASCII."""
with open('trading_bot.py', 'rb') as f:
    data = f.read()

replacements = [
    # garbled green circle emoji -> [+]
    ('"\xc3\xb0\xc5\xb8\xc5\xb8\xc2\xa2"'.encode(), b'"[+]"'),
    # garbled red circle emoji -> [-]
    ('"\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc2\xb4"'.encode(), b'"[-]"'),
    # garbled chart emoji -> [%]
    ('"\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc5\xa0"'.encode(), b'"[%]"'),
    # garbled down triangle emoji -> [S]
    ('"\xc3\xb0\xc5\xb8\xe2\x80\x9c\xc2\xbb"'.encode(), b'"[v]"'),
]

# Simpler: just find the exact byte patterns by looking at what's in the file
import re

# Replace the specific garbled emoji sequences in f-string prefixes
# ðŸŸ¢ = the garbled green circle
# ðŸ"´ = the garbled red circle
# ðŸ"» = the garbled down triangle
# ðŸ"Š = the garbled chart

text = data.decode('latin-1')  # read as latin-1 to preserve all bytes

text = text.replace('\xf0\x9f\x9f\xa2', '[+]')   # green circle U+1F7E2
text = text.replace('\xf0\x9f\x94\xb4', '[-]')   # red circle U+1F534
text = text.replace('\xf0\x9f\x93\x8a', '[~]')   # chart U+1F4CA
text = text.replace('\xf0\x9f\x94\xbb', '[v]')   # down triangle U+1F53B

with open('trading_bot.py', 'wb') as f:
    f.write(text.encode('latin-1'))

print('Done')
