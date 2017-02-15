#!/usr/bin/env python
# -*- coding:Utf-8 -*-

import json
import config

from core import bot


def file_to_json(path):
    f = open(path, 'r')
    raw = f.read()
    f.close()

    try:
        data = json.loads(raw)
    except ValueError:
        return None

    return data


if __name__ == '__main__':
    '''
        config.BOTS example

        [
            {
                'owner_id': 1,
                'use_2fa': True,
                'only_use_special_emails': False,
                'data_path': 'data/bot.json'
            }
        ]
    '''

    for BOT in config.BOTS:
        data = file_to_json(BOT['data_path'])

        delivery_bot = bot.DeliveryBot(
            BOT['owner_id'],
            data['account_name'],
            data['password'],
            data['shared_secret'],
            use_2fa=BOT['use_2fa']
        )

        delivery_bot.web_account.acquire_lock()

        if delivery_bot.web_account.lock_is_present():
            bot.log.info(
                u'Cannot init session for {}. Lock is present'.format(
                    data['account_name']
                )
            )

            continue

        delivery_bot.web_account.init_session()

        delivery_bot.track_gifts()
        delivery_bot.accept_gifts()
        delivery_bot.send_gifts(only_use_special_emails=BOT['only_use_special_emails'])

        delivery_bot.release_lock()
