"""Local, bounded QR encoding for short event/profile URLs.

QR Model 2 version 5-L, byte mode, mask 0: 106 ASCII bytes maximum.
One Reed-Solomon block: 108 data + 26 check codewords. Fixed format avoids
third-party tracking URLs and a new runtime dependency. Independent encoder
golden tests protect every module; no logo or decorative changes permitted.
"""


def _multiply(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
    return result


def _codewords(data):
    bits = '0100' + f'{len(data):08b}' + ''.join(f'{c:08b}' for c in data)
    bits += '0' * min(4, 864 - len(bits))
    bits += '0' * (-len(bits) % 8)
    words = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    while len(words) < 108:
        words.append(0xEC if (len(words) - len(bits) // 8) % 2 == 0 else 0x11)
    generator = [1]
    power = 1
    for _ in range(26):
        next_poly = [0] * (len(generator) + 1)
        for index, coefficient in enumerate(generator):
            next_poly[index] ^= coefficient
            next_poly[index + 1] ^= _multiply(coefficient, power)
        generator = next_poly
        power = _multiply(power, 2)
    remainder = words + [0] * 26
    for index in range(108):
        coefficient = remainder[index]
        for offset, value in enumerate(generator):
            remainder[index + offset] ^= _multiply(coefficient, value)
    return words + remainder[-26:]


def qr_matrix(text):
    try:
        data = text.encode('ascii')
    except (AttributeError, UnicodeEncodeError):
        raise ValueError('QR links must contain ASCII characters.') from None
    if not 1 <= len(data) <= 106:
        raise ValueError('QR links must be between 1 and 106 bytes.')
    size = 37
    matrix = [[None] * size for _ in range(size)]

    def put(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            matrix[y][x] = bool(dark)

    for x0, y0 in [(0, 0), (size - 7, 0), (0, size - 7)]:
        for y in range(-1, 8):
            for x in range(-1, 8):
                dark = (0 <= x <= 6 and 0 <= y <= 6
                        and (x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4)))
                put(x0 + x, y0 + y, dark)
    for y in range(-2, 3):
        for x in range(-2, 3):
            put(30 + x, 30 + y, max(abs(x), abs(y)) != 1)
    for i in range(8, size - 8):
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)

    # L correction + mask 0, BCH encoded and XOR-masked by 0x5412.
    format_bits = 0x77C4
    for i in range(15):
        value = (format_bits >> i) & 1
        y = i if i < 6 else (i + 1 if i < 8 else size - 15 + i)
        put(8, y, value)
        x = size - i - 1 if i < 8 else (7 if i == 8 else 14 - i)
        put(x, 8, value)
    put(8, size - 8, True)

    stream = ''.join(f'{word:08b}' for word in _codewords(data))
    cursor, upward, right = 0, True, size - 1
    while right > 0:
        if right == 6:
            right -= 1
        for y in (range(size - 1, -1, -1) if upward else range(size)):
            for x in (right, right - 1):
                if matrix[y][x] is not None:
                    continue
                value = cursor < len(stream) and stream[cursor] == '1'
                put(x, y, value ^ ((x + y) % 2 == 0))
                cursor += 1
        upward = not upward
        right -= 2
    return matrix


def qr_svg(text):
    matrix = qr_matrix(text)
    paths = ''.join(f'M{x + 4},{y + 4}h1v1h-1z'
                    for y, row in enumerate(matrix) for x, dark in enumerate(row) if dark)
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45" '
            'role="img" aria-label="Profile or event QR code" shape-rendering="crispEdges">'
            '<rect width="45" height="45" fill="white"/>'
            f'<path d="{paths}" fill="black"/></svg>')
