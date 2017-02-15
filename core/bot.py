#!/usr/bin/env python
# -*- coding:Utf-8 -*-

import re
import json
import time
import base64
import requests
import datetime

import steam.guard
import steam.webauth
from steam.enums import EResult

import enums
import config
from core import items

from steamcommerce_api.api import logger
from steamcommerce_api.api import delivery
from steamcommerce_api.api import userrequest
from steamcommerce_api.api import paidrequest
from steamcommerce_api.api import asset as asset_api
from steamcommerce_api.enums import EAssetHistoryState

from steamcommerce_api.cache import cache
from steamcommerce_api.caching import cache_layer

log = logger.Logger('steamcommerce.delivery.bot', 'steamcommerce.delivery.bot.log').get_logger()


class WebAccount(object):
    def __init__(self, account_name, password, shared_secret, use_2fa=True):
        self.account_name = account_name
        self.password = password
        self.shared_secret = shared_secret
        self.use_2fa = use_2fa

        self.lock_cache_key = 'bot/lock/{0}'.format(self.account_name)

    def lock_is_present(self):
        return bool(cache.get(self.lock_cache_key) or 0)

    def acquire_lock(self):
        cache.set(self.lock_cache_key, 1)

    def release_lock(self):
        cache.delete(self.lock_cache_key)

    def init_session(self):
        log.info(
            u'Initializing session for account_name {0}. USE 2FA: {1}'.format(
                self.account_name,
                'YES' if self.use_2fa else 'NO'
            )
        )

        user = steam.webauth.WebAuth(self.account_name, self.password)

        if self.use_2fa:
            log.info(u'Generating 2FA Code for {}'.format(self.account_name))

            twofactor_code = self.generate_two_factor_code()

            log.info(u'Received 2FA code {}'.format(twofactor_code))
            log.info(u'Logging into account {}'.format(self.account_name))

            session = user.login(twofactor_code=twofactor_code)
        else:
            log.info(u'Logging into account {}'.format(self.account_name))
            session = user.login()

        log.info(u'Logged in, getting store sites for cookie setting')

        session.get('http://store.steampowered.com')
        session.get('https://store.steampowered.com')

        self.session = session

        log.info(u'Session for account name {} has been set'.format(self.account_name))

        return True

    def get_steam_id_from_cookies(self):
        return self.session.cookies.get('steamLogin', domain='steamcommunity.com').rsplit('%7C')[0]

    def generate_two_factor_code(self):
        return steam.guard.generate_twofactor_code_for_time(
            base64.b64decode(self.shared_secret),
            time.time()
        )

    def get_steam_inventory(self, steam_id, app_id, context_id, language='english', count=5000):
        try:
            req = self.session.get(
                'http://steamcommunity.com/inventory/{0}/{1}/{2}?l={3}&count={4}'.format(
                    steam_id,
                    app_id,
                    context_id,
                    language,
                    count,
                    timeout=config.INVENTORY_DEFAULT_TIMEOUT_SECONDS
                )
            )
        except requests.exceptions.Timeout:
            return enums.WebAccountResult.Timeout
        except Exception, e:
            log.error(
                u'Unable to retrieve inventory app_id {0} context_id {1} for steamid {2}. Raised: {3}'.format(
                    app_id,
                    context_id,
                    steam_id,
                    e
                )
            )

            return enums.WebAccountResult.UnknownException

        try:
            data = req.json()
        except ValueError:
            log.error(u'Could not serialize response')

            return enums.WebAccountResult.ResponseNotSerializable

        return data

    def item_description_is_sent(self, owner_descriptions):
        description_values = [x.get('value') for x in owner_descriptions or []]

        return 'Sent to' in ''.join(description_values)

    def get_description_indexes(self, descriptions, filter_sent=True):
        # The description index dictionary is to point from a classid_instanceid key to a description

        description_indexes = {}
        log.info(u'Generating description indexes for {} descriptions'.format(len(descriptions)))

        for description in descriptions:
            item_is_sent = self.item_description_is_sent(description.get('owner_descriptions'))

            if filter_sent and item_is_sent:
                continue
            elif not filter_sent and not item_is_sent:
                continue

            classid_instanceid = '{0}_{1}'.format(
                description.get('classid'),
                description.get('instanceid')
            )

            description_indexes[classid_instanceid] = dict(description)

        return description_indexes

    @cache_layer.get_or_cache('delivery/validateunpack/{0}')
    def get_item_info_from_unpack(self, assetid):
        log.info(u'Validate unpack for assetid {}'.format(assetid))

        try:
            req = self.session.post(
                'http://steamcommunity.com/gifts/{}/validateunpack'.format(assetid),
                data={
                    'sessionid': self.session.cookies.get(
                        'sessionid',
                        domain='steamcommunity.com'
                    )
                }
            )
        except requests.exceptions.Timeout:
            return enums.WebAccountResult.Timeout.value
        except Exception, e:
            log.error(
                u'Unable to call item unpack for assetid {0} Raised: {1}'.format(
                    assetid,
                    e
                )
            )

            return enums.WebAccountResult.UnknownException.value
        try:
            data = req.json()
        except ValueError:
            log.error(u'Could not serialize response')

            return enums.WebAccountResult.ResponseNotSerializable.value

        if not data.get('success'):
            return enums.WebAccountResult.Failed.value

        return {
            'type': 'sub',
            'id': data.get('packageid')
        }

    def get_item_info(self, actions, assetid):
        item_info = {}

        if actions and len(actions):
            # Get item info from actions

            for action in actions:
                action_name = action.get('name')
                action_link = action.get('link')

                if action_name != 'View in store':
                    continue

                item_matches = re.findall(
                    r'http://store.steampowered.com/(.*?)/([0-9]+)/',
                    action_link,
                    re.DOTALL
                )

                if not len(item_matches):
                    log.error(u'Could not match item information from link {}'.format(action_link))

                    break

                item_match = item_matches[0]

                item_info['type'] = item_match[0]
                item_info['id'] = item_match[1]
        else:
            # Get item info from unpack

            item_info = self.get_item_info_from_unpack(assetid)

        return item_info

    def get_inventory_items(self, filter_sent=True):
        steam_id = self.get_steam_id_from_cookies()
        app_id = 753
        context_id = 1

        log.info(u'Getting steam gift inventory for {}'.format(self.account_name))

        inventory_data = self.get_steam_inventory(steam_id, app_id, context_id)

        if type(inventory_data) == enums.WebAccountResult:
            return inventory_data

        if not inventory_data.get('success'):
            return enums.WebAccountResult.Failed

        log.info(
            u'Steam inventory total inventoy count is {}'.format(
                inventory_data.get('total_inventory_count')
            )
        )

        description_indexes = self.get_description_indexes(
            inventory_data.get('descriptions'),
            filter_sent=filter_sent
        )

        items = {}

        log.info(u'Parsing steam inventory assets')

        for asset in inventory_data.get('assets'):
            classid_instanceid = '{0}_{1}'.format(
                asset.get('classid'),
                asset.get('instanceid')
            )

            if classid_instanceid not in description_indexes.keys():
                continue

            description = description_indexes[classid_instanceid]

            item_info = self.get_item_info(
                description.get('actions'),
                asset.get('assetid')
            )

            if type(item_info) == enums.WebAccountResult:
                log.error(
                    u'Failed to retrieve item information for {0}, received {1}'.format(
                        asset.get('assetid'),
                        repr(item_info)
                    )
                )

                continue

            # Wipe variables from previous iterations

            app_id = None
            sub_id = None

            if item_info.get('type') == 'app':
                # To be absolutely sure get apps from unpacking their assetid and the match it against store_sub_id

                unpack_info = self.get_item_info_from_unpack(asset.get('assetid'))

                if type(unpack_info) != dict:
                    log.error(u'Failed to unpack item information for {}'.format(asset.get('assetid')))

                    continue

                sub_id = unpack_info.get('id')
            elif item_info.get('type') == 'sub':
                sub_id = item_info.get('id')

            if str(sub_id) not in items.keys():
                items[str(sub_id)] = []

            items[str(sub_id)].append({
                'name': description.get('name'),
                'assetid': asset.get('assetid')
            })

        return items

    def decline_gift(self, gift_id, sender_steam_id, decline_note='Auto-declined'):
        try:
            req = self.session.post(
                'http://steamcommunity.com/gifts/{0}/decline'.format(gift_id),
                data={
                    'note': decline_note,
                    'steamid_sender': sender_steam_id,
                    'sessionid': self.session.cookies.get(
                        'sessionid',
                        domain='steamcommunity.com'
                    )
                }
            )
        except requests.exceptions.Timeout:
            log.error(
                u'Could not decline gift with gift id {}. Request timed out'.format(gift_id)
            )

            return enums.WebAccountResult.Timeout
        except Exception, e:
            log.error(
                u'Could not decline gift with gift id {0}. Raised {1}'.format(gift_id, e)
            )

            return enums.WebAccountResult.UnknownException

        if req.status_code != 200:
            log.error(
                u'Could not decline gift with gift id {0}. Received {1}'.format(
                    gift_id,
                    req.status_code
                )
            )

            return enums.WebAccountResult.Failed

        try:
            data = req.json()
        except ValueError:
            log.error(u'Could not serialize response, received {}'.format(req.text))

            return enums.WebAccountResult.ResponseNotSerializable

        response = EResult(data.get('success'))

        if response == EResult.OK:
            log.info(u'Declined gift succesfuly')

            tracking_id = asset_api.AssetTracking().get_or_create(gift_id)

            asset_api.AssetTracking().create_history(tracking_id, EAssetHistoryState.ReturnedToSender)

        return response

    def accept_gift(self, gift_id, sender_steam_id):
        try:
            req = self.session.post(
                'http://steamcommunity.com/gifts/{0}/accept'.format(
                    gift_id
                ),
                data={
                    'sessionid': self.session.cookies.get(
                        'sessionid',
                        domain='steamcommunity.com'
                    )
                }
            )
        except requests.exceptions.Timeout:
            log.error(
                u'Could not accept gift with gift_id {}. Request timed out'.format(gift_id)
            )

            return enums.WebAccountResult.Timeout
        except Exception, e:
            log.error(
                u'Could not accept gift with gift_id {0}. Raised {1}'.format(gift_id, e)
            )

            return enums.WebAccountResult.UnknownException

        if req.status_code != 200:
            log.error(
                u'Could not decline gift with gift_id {0}. Received {1}'.format(
                    gift_id,
                    req.status_code
                )
            )

            return enums.WebAccountResult.Failed

        try:
            data = req.json()
        except ValueError:
            log.error(u'Could not serialize response, received {}'.format(req.text))

            return enums.WebAccountResult.ResponseNotSerializable

        result = EResult(data.get('success'))

        if result == EResult.OK:
            assetid = data.get('gidgiftnew')

            log.info(u'Accepted gift succesfuly. New assetid is {}'.format(assetid))

            tracking_id = asset_api.AssetTracking().get_or_create(assetid)

            asset_api.AssetTracking().create_history(
                tracking_id,
                EAssetHistoryState.ReturnedToSender
            )

            asset_api.AssetTracking().update_tracking(
                id=tracking_id,
                received_from_steam_id=sender_steam_id
            )

        return result

    def get_pending_gifts(self):
        try:
            req = self.session.get('https://steamcommunity.com/my/inventory')
        except requests.exceptions.Timeout:
            log.error(
                u'Unable to get user inventory for account {}. Request timed out'.format(
                    self.account_name
                )
            )

            return enums.WebAccountResult.Timeout
        except Exception, e:
            log.error(
                u'Unable to get user inventory for account {0}. Raised {1}'.format(
                    self.account_name,
                    e
                )
            )

            return enums.WebAccount.UnknownException

        if req.status_code != 200:
            log.error(
                u'Unable to get user inventory for account {0}. Request received {1}'.format(
                    self.account_name,
                    req.status_code
                )
            )

            return enums.WebAccountResult.Failed

        inventory_matches = items.SteamGiftInventory.all_from(req.text)

        if not len(inventory_matches):
            log.info(u'Crawler was unable to find any pending gifts')

            return enums.WebAccountResult.Failed

        inventory = inventory_matches[0]

        if not len(inventory.gifts):
            log.info(u'Crawler was unable to find any pending gifts')

            return enums.WebAccountResult.Failed

        return inventory.gifts

    def delivery_is_overdue(self, relation_type, relation_id):
        delivery_config = delivery.Delivery().get_delivery_config()

        if relation_type == 'A':
            relation = userrequest.UserRequest()._get_relation_id(relation_id)
            time_diff = datetime.datetime.now() - (relation.request.paid_date or datetime.datetime.now())

        elif relation_type == 'C':
            relation = paidrequest.PaidRequest()._get_relation_id(relation_id)
            time_diff = datetime.datetime.now() - (relation.request.date or datetime.datetime.now())

        is_timely_overdue = (time_diff.total_seconds() / 60 / 60) > delivery_config.overdue_hour_courtesy

        return delivery_config.generate_overdue_codes and is_timely_overdue

    def get_delivery_message(self, relation_type, relation_id):
        is_overdue = self.delivery_is_overdue(relation_type, relation_id)
        delivery_message = delivery.Delivery().get_random_message(is_overdue=is_overdue)

        if relation_type == 'A':
            relation = userrequest.UserRequest()._get_relation_id(relation_id)
        elif relation_type == 'C':
            relation = paidrequest.PaidRequest()._get_relation_id(relation_id)

        user = relation.request.user
        request_custom_id = '{0}-{1}'.format(relation_type, relation.request.id)

        if is_overdue and delivery_message.is_overdue:
            overdue_code = delivery.Delivery().generate_overdue_code(relation_type, relation_id)

            delivery_message.giftee_name = delivery_message.giftee_name.format(user.name)
            delivery_message.gift_message = delivery_message.gift_message.format(
                user.name,
                overdue_code,
                request_custom_id
            )
        else:
            delivery_message.giftee_name = delivery_message.giftee_name.format(user.name)
            delivery_message.gift_message = delivery_message.gift_message.format(user.name, request_custom_id)

        return delivery_message

    def send_gift(self, assetid, email, relation_type, relation_id):
        REFERER = 'https://store.steampowered.com/checkout/sendgift/{0}'.format(assetid)
        USER_AGENT = 'Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:44.0) Gecko/20100101 Firefox/44.0'

        delivery_message = self.get_delivery_message(relation_type, relation_id)

        req = self.session.post(
            'https://store.steampowered.com/checkout/sendgiftsubmit/',
            data={
                'SessionID': self.session.cookies.get(
                    'sessionid',
                    domain='store.steampowered.com'
                ),
                'GiftGID': assetid,
                'GiftMessage': delivery_message.gift_message,
                'GiftSignature': delivery_message.gift_signature,
                'GiftSentiment': delivery_message.gift_sentiment,
                'GifteeName': delivery_message.giftee_name,
                'GifteeAccountID': '0',
                'ScheduledSendOnDate': '0',
                'GifteeEmail': email
            },
            headers={
                'Referer': REFERER,
                'User-Agent': USER_AGENT
            }
        )

        if req.status_code != 200:
            log.info(u'Gift submit received status code {1}'.format(assetid, req.status))

            return EResult.Fail

        try:
            data = req.json()
        except ValueError:
            log.error(u'Could not serialize response, received {}'.format(req.text))

            return EResult.Fail

        result = EResult(data.get('success'))

        if result == EResult.OK:
            sender_steam_id = self.get_steam_id_from_cookies()

            tracking_id = asset_api.AssetTracking().get_or_create(
                assetid,
                relation_type=relation_type,
                relation_id=relation_id
            )

            asset_api.AssetTracking().create_history(tracking_id, EAssetHistoryState.Sent)

            asset_api.AssetTracking().update_tracking(
                id=tracking_id,
                sent_to_email=email,
                sent_from_steam_id=sender_steam_id
            )

        return result


class DeliveryBot(object):
    def __init__(self, owner_id, account_name, password, shared_secret, use_2fa=True):
        self.web_account = WebAccount(account_name, password, shared_secret, use_2fa=use_2fa)
        self.owner_id = owner_id

    def get_pending_deliveries(self):
        paidrequest_relations = paidrequest.PaidRequest().get_pending_relations(self.owner_id)
        userrequest_relations = userrequest.UserRequest().get_pending_relations(self.owner_id)

        log.info(
            u'Pending paidrequest relations: {}'.format(
                paidrequest_relations.count()
            )
        )

        log.info(
            u'Pending userrequest relations: {}'.format(
                userrequest_relations.count()
            )
        )

        unsent_items = self.web_account.get_inventory_items(filter_sent=True)
        unsent_items_count = sum([len(unsent_items[x]) for x in unsent_items.keys()])

        log.info(u'Found {} unsent gifts'.format(unsent_items_count))

        commited_assetids = []
        pending_assets_delivery = []

        for relation in paidrequest_relations:
            product = relation.product

            if product.app_id and product.store_sub_id:
                product_sub_id = product.store_sub_id
            elif product.sub_id:
                product_sub_id = product.sub_id
            else:
                log.error(u'Product id {} does not contain a store_sub_id'.format(product.id))

                continue

            if not unsent_items.get(product_sub_id):
                continue

            for item in unsent_items[product_sub_id]:
                if item.get('assetid') in commited_assetids:
                    continue

                commited_assetids.append(item.get('assetid'))

                pending_assets_delivery.append({
                    'relation_type': 'C',
                    'name': item.get('name'),
                    'relation_id': relation.id,
                    'assetid': item.get('assetid'),
                    'email': relation.request.user.email,
                    'request_id': relation.request.id
                })

                break

        for relation in userrequest_relations:
            product = relation.product

            if product.app_id and product.store_sub_id:
                product_sub_id = product.store_sub_id
            elif product.sub_id:
                product_sub_id = product.sub_id
            else:
                log.error(u'Product id {} does not contain a sub_id'.format(product.id))

                continue

            if not unsent_items.get(product_sub_id):
                continue

            for item in unsent_items[product_sub_id]:
                if item.get('assetid') in commited_assetids:
                    continue

                commited_assetids.append(item.get('assetid'))

                pending_assets_delivery.append({
                    'relation_type': 'A',
                    'name': item.get('name'),
                    'relation_id': relation.id,
                    'assetid': item.get('assetid'),
                    'email': relation.request.user.email,
                    'request_id': relation.request.id
                })

                break

        return pending_assets_delivery

    def get_special_email(self, relation_type, relation_id, request_id):
        return 'entregas+{0}{1}{2}@extremegaming-arg.com.ar'.format(
            relation_id,
            relation_type,
            request_id
        )

    def send_gifts(self, only_use_special_emails=False):
        if not self.web_account:
            return None

        pending_gifts = self.get_pending_deliveries()

        for gift in pending_gifts:
            name = gift.get('name')
            assetid = gift.get('assetid')
            request_id = gift.get('request_id')
            relation_id = gift.get('relation_id')
            relation_type = gift.get('relation_type')

            if only_use_special_emails:
                email = self.get_special_email(relation_type, relation_id, request_id)
            else:
                email = gift.get('email')

            log.info(
                u'Sending gift {0} assetid {1} to {2} for request {3}-{4} relation {5}'.format(
                    name,
                    assetid,
                    email,
                    relation_type,
                    request_id,
                    relation_id
                )
            )

            result = self.web_account.send_gift(assetid, email, relation_type, relation_id)

            if EResult(result) != EResult.OK and not only_use_special_emails:
                log.info(u'Sending failed, received {}'.format(repr(EResult(result))))

                email = self.get_special_email(relation_type, relation_id, request_id)

                log.info(
                    u'Sending gift {0} assetid {1} to {2} for request {3}-{4} relation {5}'.format(
                        name,
                        assetid,
                        email,
                        relation_type,
                        request_id,
                        relation_id
                    )
                )

                result = self.web_account.send_gift(assetid, email, relation_type, relation_id)

            if EResult(result) != EResult.OK:
                log.info(u'Sending failed, received {}'.format(repr(EResult(result))))

                continue

            log.info(u'Sent gift {0} with assetid {1} succesfuly'.format(name, assetid))

            if relation_type == 'A':
                relation = userrequest.UserRequest()._get_relation_id(relation_id)
                userrequest.UserRequest().set_sent(relation_id, gid=assetid)

                if not relation.request.assigned:
                    log.info(
                        u'Assigning user id {0} to request {1}-{2}'.format(self.owner_id, relation_type, request_id)
                    )

                    userrequest.UserRequest().assign(request_id, self.owner_id)

            elif relation_type == 'C':
                relation = paidrequest.PaidRequest()._get_relation_id(relation_id)
                paidrequest.PaidRequest().set_sent(relation_id, gid=assetid)

                if not relation.request.assigned:
                    log.info(
                        u'Assigning user id {0} to request {1}-{2}'.format(self.owner_id, relation_type, request_id)
                    )

                    paidrequest.PaidRequest().assign(request_id, self.owner_id)

        paidrequests = paidrequest.PaidRequest().get_paid_query()
        userrequests = userrequest.UserRequest().get_paid_query()

        for paidrequest_data in paidrequests:
            if (
                paidrequest_data.products.filter(sent=False).count() == 0 and
                paidrequest_data.assigned.id == self.owner_id
            ):
                log.info(u'Accepting request C-{}'.format(paidrequest_data.id))
                paidrequest.PaidRequest().accept_paidrequest(paidrequest_data.id, self.owner_id)

        for userrequest_data in userrequests:
            if (
                userrequest_data.products.filter(sent=False).count() == 0 and
                paidrequest_data.assigned.id == self.owner_id
            ):
                log.info(u'Accepting request A-{}'.format(userrequest_data.id))
                userrequest.UserRequest().accept_userrequest(userrequest_data.id, self.owner_id)

    def accept_gifts(self):
        gifts = self.web_account.get_pending_gifts()

        if type(gifts) == enums.WebAccountResult:
            log.error(
                u'Could not accept pending gifts. Received {}'.format(repr(gifts))
            )

            return gifts

        log.info(u'Found {0} pending gifts'.format(len(gifts)))

        for gift in gifts:
            if not gift.gift_javascript:
                log.error(u'Unable to find gift javascript object')

                continue

            matches = re.findall(
                r'BuildHover\( .*, ({.*}), .*\)',
                gift.gift_javascript,
                re.DOTALL
            )

            if not len(matches):
                log.error(u'Regex failed to retrieve gift javascript object')

                continue

            gift_object = json.loads(matches[0])

            log.info(
                u'Found pending gift {0} from {1} ({2})'.format(
                    gift_object.get('name'),
                    gift.from_username,
                    gift.from_link
                )
            )

            if not gift.accept_button or 'UnpackGift' in gift.accept_button:
                log.info(u'Gift cannot be accepted to inventory')

                sender_steam_id_matches = re.findall(
                    r'http://steamcommunity\.com/profiles/([0-9]+)',
                    gift.from_link,
                    re.DOTALL
                )

                if not len(sender_steam_id_matches):
                    log.error(u'Regex failed to retrieve steam sender id')

                    continue

                sender_steam_id = sender_steam_id_matches[0]

                log.info(
                    u'Declining gift id {0} to sender id {1}'.format(
                        gift_object.get('id'),
                        sender_steam_id
                    )
                )

                result = self.web_account.decline_gift(
                    gift_object.get('id'),
                    sender_steam_id
                )

                if result != EResult.OK:
                    log.error(
                        u'Could not accept gift id {0}. Received {1}'.format(
                            gift_object,
                            repr(result)
                        )
                    )

            elif 'ShowAcceptGiftOptions' in gift.accept_button:
                log.info(
                    u'Accepting gift id {0} to gift inventory'.format(
                        gift_object.get('id')
                    )
                )

                result = self.web_account.accept_gift(
                    gift_object.get('id'),
                    sender_steam_id
                )

                if result != EResult.OK:
                    log.error(
                        u'Could not accept gift id {0}. Received {1}'.format(
                            gift_object,
                            repr(result)
                        )
                    )

    def track_gifts(self):
        sent_items = self.web_account.get_inventory_items(filter_sent=False)
        unsent_items_count = sum([len(sent_items[x]) for x in sent_items.keys()])

        log.info(u'Found {} sent gifts'.format(unsent_items_count))

        assetids = []

        for sub_id in sent_items.keys():
            assets = sent_items[sub_id]

            for asset in assets:
                assetids.append(asset.get('assetid'))

        uncompleted_trackings = asset_api.AssetTracking().get_uncompleted_trackings()
        current_steam_id = self.web_account.get_steam_id_from_cookies()

        for tracking in uncompleted_trackings:
            if tracking.sent_from_steam_id == current_steam_id and tracking.assetid not in assetids:
                log.info(
                    u'Assetid {0} is no longer on sender\'s inventory'.format(
                        tracking.assetid
                    )
                )

                asset_api.AssetTracking().create_history(
                    tracking.id,
                    EAssetHistoryState.MissingFromInventory
                )

                asset_api.AssetTracking().update_tracking(id=tracking.id, completed=True)
