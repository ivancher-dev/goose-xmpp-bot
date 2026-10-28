"""Проверка ручных отправок и общие ключи live/Carbons/MAM без текста в журнале."""

from copy import deepcopy
import hashlib
import xml.etree.ElementTree as ET

from slixmpp import JID, Message

from .config import ConfigError, bare_jid
from .omemo import LEGACY_NS

CARBONS = 'urn:xmpp:carbons:2'
FORWARD = 'urn:xmpp:forward:0'
DELAY = 'urn:xmpp:delay'
SID = 'urn:xmpp:sid:0'


def social_only(stanza):
    # Реакции могут содержать fallback-body, но не являются текстом собеседника.
    if stanza.xml.find('{urn:xmpp:reactions:0}reactions') is not None:
        return True
    return (not stanza['body'].strip()
            and not any(c.tag.endswith('}encrypted') for c in stanza.xml)
            and any(c.tag.startswith('{http://jabber.org/protocol/chatstates}') for c in stanza.xml))


def ordinary_message(stanza, *, delayed=False):
    if stanza['type'] not in {'chat', 'normal'} or social_only(stanza):
        return False
    for element in stanza.xml.iter():
        tag = element.tag
        if (tag.startswith(('{urn:xmpp:carbons:', '{urn:xmpp:mam:', '{urn:xmpp:forward:'))
                or tag in {'{urn:xmpp:receipts}received', '{urn:xmpp:chat-markers:0}displayed'}
                or (not delayed and tag in {f'{{{DELAY}}}delay', '{jabber:x:delay}x'})):
            return False
    return True


def owner_stanza(stanza, own_jid, own_resource, allowed):
    """Только другая сессия аккаунта Гуся; чужая sent-оболочка не авторизует from."""
    try:
        inner = stanza
        carbons = [c for c in stanza.xml if c.tag.startswith('{urn:xmpp:carbons:')]
        if carbons:
            if (str(stanza['from']) != own_jid or bare_jid(str(stanza['to'])) != own_jid
                    or stanza['type'] not in {'chat', 'normal'} or len(carbons) != 1
                    or carbons[0].tag != f'{{{CARBONS}}}sent'):
                return None
            children = list(carbons[0])
            if len(children) != 1 or children[0].tag != f'{{{FORWARD}}}forwarded':
                return None
            forwarded = list(children[0])
            if len(forwarded) != 1 or forwarded[0].tag != '{jabber:client}message':
                return None
            if (sum(e.tag.startswith('{urn:xmpp:carbons:') for e in stanza.xml.iter()) != 1
                    or sum(e.tag == f'{{{FORWARD}}}forwarded' for e in stanza.xml.iter()) != 1):
                return None
            # Внешняя задержка/архив не превращаются в живую ручную отправку.
            if any(e.tag.startswith('{urn:xmpp:mam:') or e.tag in {f'{{{DELAY}}}delay', '{jabber:x:delay}x'}
                   for e in stanza.xml.iter()):
                return None
            inner = Message(xml=deepcopy(forwarded[0]), stream=stanza.stream)
        if (bare_jid(str(inner['from'])) != own_jid or not ordinary_message(inner)
                or JID(str(inner['from'])).resource == own_resource):
            return None
        peer = bare_jid(str(inner['to']))
        if peer not in allowed or peer == own_jid:
            return None
        if not inner['body'].strip() and not any(c.tag.endswith('}encrypted') for c in inner.xml):
            return None
        return inner, peer
    except ConfigError:
        return None


def message_keys(stanza, own_jid):
    """Идентификаторы ограничены отправителем/получателем, ciphertext — запасной alias."""
    sender, recipient = bare_jid(str(stanza['from'])), bare_jid(str(stanza['to']))
    scope = f'{sender}\0{recipient}\0'
    identifiers = []
    for element in stanza.xml.findall(f'{{{SID}}}origin-id'):
        if element.get('id') and len(element.get('id')) <= 1024:
            identifiers.append('origin:' + element.get('id'))
    if stanza['id'] and len(stanza['id']) <= 1024:
        identifiers.append('id:' + stanza['id'])
    # stanza-id доверяется только собственному архиву, не произвольному by.
    for element in stanza.xml.findall(f'{{{SID}}}stanza-id'):
        if element.get('by') == own_jid and element.get('id') and len(element.get('id')) <= 1024:
            identifiers.append('server:' + element.get('by') + ':' + element.get('id'))
    encrypted = stanza.xml.find(f'{{{LEGACY_NS}}}encrypted')
    if encrypted is not None and encrypted.findtext(f'{{{LEGACY_NS}}}payload'):
        identifiers.append('cipher:' + ET.canonicalize(ET.tostring(encrypted, encoding='unicode'), rewrite_prefixes=True))
    if not identifiers:
        identifiers.append('fallback:' + ET.canonicalize(ET.tostring(stanza.xml, encoding='unicode'), rewrite_prefixes=True))
    return tuple(dict.fromkeys(hashlib.sha256((scope + value).encode()).hexdigest() for value in identifiers))
