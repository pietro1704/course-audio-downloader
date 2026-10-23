"""Portable local filenames; display titles and source identities stay literal."""
import hashlib
import re


def component(value):
    cleaned=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',value).rstrip(' .')
    if not cleaned or cleaned in {'.','..'}:raise ValueError('invalid-site-path')
    if re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?',cleaned):cleaned='_'+cleaned
    if len(cleaned.encode())>180:
        tail='-'+hashlib.sha256(value.encode()).hexdigest()[:12]
        while len(cleaned.encode())>160:cleaned=cleaned[:-1]
        cleaned+=tail
    return cleaned
