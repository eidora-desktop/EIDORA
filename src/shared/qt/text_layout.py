"""Bounded plain-text wrapping using Qt Unicode layout."""
from PySide6 import QtGui


def wrapped_text_lines(text, font, width, limit):
    result = []
    option = QtGui.QTextOption()
    option.setWrapMode(QtGui.QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    for paragraph in text.split("\n"):
        if not paragraph:
            result.append("")
        else:
            encoded = paragraph.encode("utf-16-le")
            layout = QtGui.QTextLayout(paragraph, font)
            layout.setTextOption(option)
            layout.beginLayout()
            while len(result) < limit:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                start = line.textStart() * 2
                end = start + line.textLength() * 2
                result.append(encoded[start:end].decode("utf-16-le").rstrip())
            layout.endLayout()
        if len(result) >= limit:
            break
    return result

