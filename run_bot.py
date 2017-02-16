#!/usr/bin/env python
# -*- coding:Utf-8 -*-

import os
import json
import rollbar
import config

from core import bot

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


def file_to_json(path):
    f = open(os.path.join(os.getcwd(), path), 'r')
    raw = f.read()
    f.close()

    try:
        data = json.loads(raw)
    except ValueError:
        return None

    return data


def run_bot():
    for BOT in config.BOTS:
        data = file_to_json(BOT['data_path'])

        delivery_bot = bot.DeliveryBot(
            BOT['owner_id'],
            data['account_name'],
            data['password'],
            data['shared_secret'],
            use_2fa=BOT['use_2fa']
        )

        if delivery_bot.web_account.lock_is_present():
            bot.log.info(
                u'Cannot init session for {}. Lock is present'.format(
                    data['account_name']
                )
            )

            continue

        delivery_bot.web_account.acquire_lock()
        delivery_bot.web_account.init_session()

        delivery_bot.track_gifts()
        delivery_bot.accept_gifts()
        delivery_bot.send_gifts(only_use_special_emails=BOT['only_use_special_emails'])

        delivery_bot.web_account.release_lock()


if __name__ == '__main__':
    rollbar.init(config.ROLLBAR_TOKEN, 'production')  # access_token, environment

    try:
        run_bot()
    except IOError:
        rollbar.report_message('Got an IOError in the main loop', 'warning')
    except:
        # catch-all

        rollbar.report_exc_info()
