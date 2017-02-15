#!/usr/bin/env python
# -*- coding:Utf-8 -*-

import demiurge


class SteamGift(demiurge.Item):
    gift_javascript = demiurge.TextField(
        selector='div:last-child div.pending_gift_leftcol script'
    )

    from_link = demiurge.AttributeValueField(
        selector='div:last-child div.pending_gift_rightcol p:first-child a',
        attr='href'
    )

    from_username = demiurge.TextField(
        selector='div:last-child div.pending_gift_rightcol p:first-child a'
    )

    accept_button = demiurge.AttributeValueField(
        selector='div:last-child div.pending_gift_rightcol div.gift_controls div.gift_controls_buttons div.btn_medium:first',
        attr='onclick'
    )

    class Meta:
        selector = 'div.pending_gift'


class SteamGiftInventory(demiurge.Item):
    gifts = demiurge.RelatedItem(
        SteamGift
    )

    class Meta:
        selector = 'div#tabcontent_pendinggifts'
