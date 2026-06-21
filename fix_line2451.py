
# Fix smart quotes (U+201C/U+201D = E2 80 9C / E2 80 9D) introduced by editor
# Only in the partial sell line around line 2451
with open('trading_bot.py', 'rb') as f:
    data = bytearray(f.read())

# U+201D in UTF-8 = bytes E2 80 9D
# U+201C in UTF-8 = bytes E2 80 9C
# Replace them with 0x22 (straight double quote) followed by removing extra bytes
# We need to collapse 3 bytes -> 1 byte, so we rebuild the array

result = bytearray()
i = 0
fixes = 0
while i < len(data):
    if i + 2 < len(data) and data[i] == 0xE2 and data[i+1] == 0x80 and data[i+2] in (0x9C, 0x9D):
        result.append(0x22)  # straight double quote
        i += 3
        fixes += 1
    else:
        result.append(data[i])
        i += 1

with open('trading_bot.py', 'wb') as f:
    f.write(bytes(result))

print(f'Fixed {fixes} smart quote sequences')
