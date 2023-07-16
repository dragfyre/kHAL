#!/usr/bin/env python3.11
#coding=utf-8
from requests_toolbelt import MultipartEncoder
import requests
import sys
import os
import regex as re
import json
import shutil
import traceback
import random
import string
import math
from time import sleep, time
from datetime import datetime, timedelta
from dateutil.parser import *
from dateutil.tz import tzutc
from dotenv import load_dotenv
#from rss_parser import Parser as RSSParser
from typing import Optional, List, Dict, Union
import logging

# megahal
from megahal import *

# text/nlp parsing
from html.parser import HTMLParser

load_dotenv()

# OS stuff
KBOT_STDOUT = os.getenv("KBOT_STDOUT", "/dev/fd/0")
KBOT_STDERR = os.getenv("KBOT_STDERR", "/dev/fd/1")
KBOT_LOGLEVEL = os.getenv("KBOT_LOGLEVEL", "INFO")

# Logging stuff
logging.basicConfig(filename=KBOT_STDERR)
logger = logging.getLogger("kbot")
logger.setLevel(logging._nameToLevel[KBOT_LOGLEVEL])

# Kbin regexes
TOKEN_REGEX = re.compile('"(_csrf_token|entry_article\[_token\]|entry_comment\[_token\])"\s+value="(.+)"')
MAGAZINE_REGEX = re.compile('"entry_article\[magazine\]\[autocomplete\]".+value="([0-9]+)"\sselected="selected"')
THREAD_REGEX = re.compile('id="entry-([0-9]+)"[\s\w\-=":@>#<]+<a\s+href=".+">(.+)<\/a>[\s\w\-\/\.\(\)_="><]+short-desc"\sstyle="">\s+(.+)\s+<\/div>[\s\w\-\/\.,_=+":@>#<]+\s+datetime="(.+)"\s') # Group 1 is thread id, 2 is title, 3 is content, 4 is date posted

# env stuff
KBOT_USER = os.getenv("KBOT_USER")
KBOT_PASS = os.getenv("KBOT_PASS")
KBOT_INSTANCE = os.getenv("KBOT_INSTANCE")
KBOT_MAGAZINE = os.getenv("KBOT_MAGAZINE")
KBOT_RSS = os.getenv("KBOT_RSS")
KBOT_LANG = os.getenv("KBOT_LANG")

KBOT_FREQUENCY = max(120, int(os.getenv("KBOT_FREQUENCY", "600")))
KBOT_THREAD_CACHE_SECONDS = max(10, int(os.getenv("KBOT_THREAD_CACHE_SECONDS", "30")))

assert KBOT_USER and KBOT_PASS and KBOT_INSTANCE and KBOT_MAGAZINE and KBOT_LANG, "Environment not set up correctly!"

# MegaHAL brain stuff
DEFAULT_BRAINFILE = '.hal-kbot-brain' #os.path.join(os.environ.get('HOME', ''), '.pymegahal-brain')
DEFAULT_TRAINER = 'lolstodon.trainer'
# DEFAULT_CACHEFILE = '.hal-kbot-cache'

cache_name = ".last-updated"
logged_in = False

class MLStripper(HTMLParser): # HTML tag stripper class
    def __init__(self):
        self.reset()
        self.strict = False
        self.convert_charrefs= True
        self.fed = []
    def handle_data(self, d):
        self.fed.append(d)
    def get_data(self):
        return ''.join(self.fed)

def strip_tags(html):
    s = MLStripper()
    s.feed(html)
    return s.get_data()

def smart_truncate(content, length=350, suffix='...'):
    if len(content) <= length:
        return content
    else:
        return content[:length].rsplit(' ', 1)[0]+suffix

def snip_hashtags(content):
    r = [] # response
    ht = [] # hashtags
    c = content.split() # create array of words in content
    rp = False # repeated hashtag
    for w in c:
        if w[0] == "#": # if this is a hashtag
            if rp: # if it's the second (or more) in a row
                ht.append(w) # add to the hashtag list
                if r[-1][0] == "#": # if the last word we added to the response was a hashtag
                    del r[-1] # remove it, because it's a repeated hashtag
            else:
                r.append(w) # add the hashtag to the list (it might be a singleton)
                ht.append(w) # also add it to the hashtag list
                rp = True # We've already had one
        else:
            r.append(w) 
            if rp:  # If we were expecting a repeat (NOTE: If we're here, there must be content in ht)
                del ht[-1] # Then never mind, this one is not at the end of the toot
            rp = False # Never mind
    if len(ht) > 1: # If we've got a bunch of hashtags at the end of this toot
        # print ht
        r.append(random.choice(ht)) # Just pick one, and stick it at the end for good measure
    return " ".join(r)

def login_hook(r: requests.Response, *args, **kwargs):
    global logged_in
    if r.history and "login" in r.url:
        logger.info(f"Redirected to login page: {r.status_code} {r.url}")
        logged_in = False


def rate_limit_hook(r: requests.Response, *args, **kwargs):
    global last_request_time
    poll_latency = 1.0

    now = time()
    if now < poll_latency + last_request_time:
        sleep(min(poll_latency, poll_latency + last_request_time > now))
    last_request_time = time()

def get_session():
    global last_request_time

    last_request_time = time() - 100
    session = requests.Session()
    session.hooks['response'].append(rate_limit_hook)
    session.hooks['response'].append(login_hook)
    return session

kbin_session = get_session()

def get_csrf(response: requests.Response) -> Optional[str]:
    match = TOKEN_REGEX.search(response.text)
    if not match:
        logger.error("Could not find csrf token!")
        return None
    return match.group(2)

def login() -> bool:
    global logged_in
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/login")
    if(not (200 <= response.status_code < 300)):
        logger.error(f"Unexpected status code: {response.status_code}")
        return False
        
    _csrf_token = get_csrf(response)

    if not _csrf_token:
        return False
    
    form_data = {
        "email": KBOT_USER,
        "password": KBOT_PASS,
        "_csrf_token": _csrf_token
    }

    response = kbin_session.post(f"https://{KBOT_INSTANCE}/login", data=form_data)
    if response.status_code not in [200, 302]:
        logger.error(f"Unexpected status code: {response.status_code}")
        return False

    logged_in = True
    return True

def get_magazine(response: requests.Response) -> int:
    match = MAGAZINE_REGEX.search(response.text)
    if not match:
        logger.error("Could not find magazine id!")
        return -1
    return int(match.group(1))

def post(title: str, description: str = None, tags: Optional[List[str]] = None) -> bool:
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new/article")
    if(not (200 <= response.status_code < 300)):
        logger.error(f"Unexpected status code: {response.status_code}")
        return False
    
    _csrf_token = get_csrf(response)

    if not _csrf_token:
        return False
    
    magazine_id = get_magazine(response)

    if magazine_id == -1:
        return False

    form_data = {
        "entry_article[title]": title,
        "entry_article[body]": description if description is not None else "",
        "entry_article[magazine][autocomplete]": str(magazine_id),
        "entry_article[tags]": ",".join(tags) if tags else "",
        "entry_article[badges]": "",
        "entry_article[image]": ("", "", "application/octet-stream"),
        "entry_article[imageUrl]": "",
        "entry_article[imageAlt]": "",
        "entry_article[lang]": KBOT_LANG,
        "entry_article[submit]": "",
        "entry_article[_token]": _csrf_token
    }

    m = MultipartEncoder(
        fields=form_data
    )

    headers = {
        "Content-Type": m.content_type,
        "Origin": f"https://{KBOT_INSTANCE}",
        "Referer": f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new/article"
    }

    retries = 3
    status = 422
    while status == 422 and retries > 0:
        response = kbin_session.post(f"https://{KBOT_INSTANCE}/m/{KBOT_MAGAZINE}/new/article", data=m, headers=headers)
        status = response.status_code
        if status == 422:
            retries -= 1
            logger.debug(f"Auto retrying after delay due to 422 error... ({retries} left)")
            sleep(2)
    
    if(response.status_code not in [200, 302]):
        logger.error(f"Unexpected status code: {response.status_code} - {response.url}")
        return False

    return True

###
# Dictionary of magazine names to dictionaries containing two keys:
#    - "cached_at" -> datetime
#    - "threads" -> Dict[int, str]
#
cached_threads: Dict[str, Dict[str, Union[datetime, Dict[int, str]]]] = {}
THREAD_CACHE_TIMEOUT = timedelta(seconds=KBOT_THREAD_CACHE_SECONDS)

# Lists threads in magazine by id -> title
# Caches threads automatically for 10 to infinite seconds, configurable with .env KBOT_THREAD_CACHE_SECONDS
def list_threads(magazine: str, invalidate_cache: bool = False) -> Dict[int, str]:
    global cached_threads
    if not invalidate_cache and magazine in cached_threads and (datetime.utcnow() - cached_threads[magazine]["cached_at"]) < THREAD_CACHE_TIMEOUT:
        return cached_threads[magazine]["threads"]
    to_return = {}
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{magazine}")
    if response.status_code != 200:
        logger.error(f"Got unexpected status while retrieving threads: {response.status_code}")
        return to_return
    matches: List[re.Match[str]] = THREAD_REGEX.finditer(response.text)
    for match in matches:
        thread_id = int(match.group(1))
        title = match.group(2)
        #content = match.group(3)
        #date = match.group(4)
        to_return[thread_id] = {'title':title } #, 'content':content, 'date':date}

    cached_threads[magazine] = {
        "cached_at": datetime.utcnow(),
        "threads": to_return
    }

    logger.debug("to_return: %s" % to_return)
    return to_return

def post_toplevel_comment(magazine: str, thread_id: int, body: str, lang: str) -> bool:
    response = kbin_session.get(f"https://{KBOT_INSTANCE}/m/{magazine}/t/{thread_id}")
    if response.status_code != 200:
        logger.error(f"Unexpected status code while retrieving thread: {response.status_code}")
        return False
    
    csrf_token = get_csrf(response)
    if csrf_token is None:
        logger.error("Could not find csrf_token while posting comment!")
        return False
    
    form_data = {
        "entry_comment[body]": body,
        "entry_comment[image]": ("", "", "application/octet-stream"),
        "entry_comment[imageUrl]": "",
        "entry_comment[imageAlt]": "",
        "entry_comment[lang]": KBOT_LANG,
        "entry_comment[submit]": "",
        "entry_comment[_token]": csrf_token
    }

    m = MultipartEncoder(fields=form_data)

    headers = {
        "Content-Type": m.content_type,
        "Origin": f"https://{KBOT_INSTANCE}",
        "Referer": f"https://{KBOT_INSTANCE}/m/{magazine}/t/{thread_id}"
    }

    retries = 3
    status = 422
    while status == 422 and retries > 0:
        response = kbin_session.post(f"https://{KBOT_INSTANCE}/m/{magazine}/t/{thread_id}/-/comment", data=m, headers=headers)
        status = response.status_code
        if status == 422:
            retries -= 1
            logger.debug(f"Auto retrying after delay due to 422 error... ({retries} left)")
            sleep(2)
    
    if(response.status_code not in [200, 302]):
        logger.error(f"Unexpected status code while adding comment: {response.status_code} - {response.url}")
        return False

    return True


def main():
    "Main program loop."
    global logged_in
    # Initialize loop helpers
    learn = True
    toot = True
    train = False
    reset = False
    brain = True
    skipfirst = False
    brainnotfound = False
    learned = []

    hellos = [
        'hello','hi','yo','hey','whassup?','hey whassup?','yo whassup?','sup?','sup bro?','hello there','hi there',
        "what's up?","what's going on?","what's cooking?","what's happening?","what's new?","what are you doing?",
        "what are you thinking?","where are you?","where have you been?","who's that?","who are you?",
        "what do you think of it?","how do you feel about that?","how are you feeling today?","what's good in the hood?",
        "what's new with the crew?","how's your life going?","how's your day been?","tu fais quoi aujourd'hui?",
        "tu fais quoi ce soir?","was passiert jetzt?","was ist jetzt los?","qu'est-ce qui se passe?"
        ]


    if not os.path.isfile(DEFAULT_BRAINFILE):
        brainnotfound = True
        brain = False
        train = True # If there's no brain, we gotta train
        logger.info('[o] No brain found, training mode on.')
    else:
        logger.info('[o] Brain found: %s KB.' % int(os.stat(DEFAULT_BRAINFILE).st_size / 1024))

    if len(sys.argv) > 1:
        if not brainnotfound and '--reset' in sys.argv:
            reset = True
            logger.info('[o] Reset mode on.')
        elif not brainnotfound and  '--train' in sys.argv:
            train = True
            logger.info('[o] Training mode on.')
        if '--offline' in sys.argv:
            learn = False
            toot = False
            logger.info('[o] Offline mode on (No learning, no tooting).')
        if '--nolearn' in sys.argv:
            learn = False
            logger.info('[o] Learning mode off.')
        if ('--notoot' in sys.argv) or ('--notoot' in sys.argv):
            toot = False
            logger.info('[o] Tooting mode off.')
        if ('--nofirstpost' in sys.argv) or ('--nfp' in sys.argv):
            skipfirst = True
            logger.info('[o] No First Post mode on.')

    if reset:
        os.remove(DEFAULT_BRAINFILE)
        logger.info('[x] Brain deleted, muahahaa!')
        train = True
        logger.info('[o] Training mode on.')

    # Initialize MegaHAL
    hal = MegaHAL()
    logger.info('[*] MegaHAL loaded.')

    if train or '--train' in sys.argv:
        hal.train(DEFAULT_TRAINER)  # Learn from the training file
        train = False
        logger.info("[o] Training complete.")

    #if learn and brain and not reset:
        #learned = load_cache()

    while True:
        try:
            try:
                with open(cache_name) as f:
                    last_updated = parse(f.read())
            except FileNotFoundError:
                last_updated = parse("1970-01-01T00:00:00+00:00")

            if learn:
                #if not logged_in and :
                #    ready = login()
                # response = requests.get(KBOT_RSS)
                # if not (200 <= response.status_code < 300):
                #     logger.error("[x] Error %s while fetching posts to learn from." % response.status_code)
                #     sleep(KBOT_FREQUENCY)
                #     continue

                # rss_data = RSSParser.parse(response.text)
                # logger.debug(json.dumps(json.loads(rss_data.json()), indent=4))

                # logger.debug(rss_data.channel.title)

                threads = list_threads(KBOT_MAGAZINE)

                logger.debug(threads)

                for thread_id in threads:
                    if thread_id in threads:
                        continue
                    

                # for item in reversed(rss_data.channel.items):
                #     logger.debug(item.title)
                #     pub_date = parse(str(item.pub_date))
                #     logger.debug(pub_date)
                #     logger.debug(item.link)

                #     author = str(item.author)
                #     match = re.search("invalid\@example\.com \((.+)\)", author)
                #     if(match):
                #         author = match.group(1)

            result = False

            # SECTION 2: Generate text

            if not skipfirst:

                # 2a: Title

                # At which fraction should we splice the replies?
                frac = random.choice([0.2,0.25,0.3,0.35,0.35,0.4,0.4,0.4])
                # Get two replies and splice them together, for extra variety
                s1 = hal.get_reply('')
                s2 = hal.get_reply('')
                while (s1 == s2): # We got the same sentence twice, so get a new reply with different input
                    print("[_] Collision: %s" % s2)
                    r = random.choice([1,2])
                    if r == 1: # Say hello
                        s2 = hal.get_reply(random.choice(hellos))
                    if r == 2: # Use a random quote
                        s2 = hal.get_reply(random.choice(["The sun is a mass of incandescent gas, a gigantic nuclear furnace where hydrogen is built into helium at a temperature of millions of degrees.",
                                                          "You only live once, but if you do it right, once is enough.",
                                                          "If you tell the truth, you don't have to remember anything.",
                                                          "I am so clever that sometimes I don't understand a single word of what I am saying.",
                                                          "Do you have any Grey Poupon?",
                                                          "What is the airspeed velocity of an unladen swallow?",
                                                          "I ask you, what do you really think of me?"]))

                rs1 = smart_truncate(snip_hashtags(s1))
                rs2 = smart_truncate(snip_hashtags(s2))
                title = ''

                r1 = rs1.split()
                r2 = rs2.split()
                if (frac < 0.3):
                    rl = r1[:math.ceil(len(r1)*(frac + random.choice([0,0.05,0.1,0.15,0.2])))]
                    rl.extend(r2[math.ceil(len(r2)*(frac + random.choice([0,0.05,0.1,0.15,0.2]))):math.ceil(len(r2)*(frac + random.choice([0.3,0.35,0.4])))])
                    rl.extend(r1[math.ceil(len(r1)*(frac + random.choice([0.05,0.1,0.15,0.2]))):])
                else:
                    rl = r1[:math.ceil(len(r1)*(frac + random.choice([0,0.05,0.1,0.15,0.2])))]
                    rl.extend(r2[math.ceil(len(r2)*(frac + random.choice([0,0.05,0.1,0.15,0.2]))):])
                title = smart_truncate(" ".join(rl),length=150)

                logger.debug("Generated text (title): %s" % title) # Print the final reply

                #if(last_updated < pub_date):

                # 2b: Body

                # At which fraction should we splice the replies?
                frac = random.choice([0.2,0.25,0.3,0.35,0.35,0.4,0.4,0.4])
                # Get two replies and splice them together, for extra variety
                s3 = hal.get_reply('')
                s4 = hal.get_reply('')
                while (s3 == s4): # We got the same sentence twice, so get a new reply with different input
                    logger.error("[_] Collision: %s" % s2)
                    r = random.choice([1,2])
                    if r == 1: # Say hello
                        s2 = hal.get_reply(random.choice(hellos))
                    if r == 2: # Use a random quote
                        s2 = hal.get_reply(random.choice(["The sun is a mass of incandescent gas, a gigantic nuclear furnace where hydrogen is built into helium at a temperature of millions of degrees.",
                                                          "You only live once, but if you do it right, once is enough.",
                                                          "If you tell the truth, you don't have to remember anything.",
                                                          "I am so clever that sometimes I don't understand a single word of what I am saying.",
                                                          "Do you have any Grey Poupon?",
                                                          "What is the airspeed velocity of an unladen swallow?",
                                                          "I ask you, what do you really think of me?"]))

                s5 = hal.get_reply('')
                while ((s3 == s5) or (s4 == s5)): # We got the same sentence twice, so get a new reply with different input
                    logger.error("[_] Collision: %s" % s5)
                    r = random.choice([1,2])
                    if r == 1: # Say hello
                        s5 = hal.get_reply(random.choice(hellos))
                    if r == 2: # Use a random quote
                        s5 = hal.get_reply(random.choice(["Let any fish who meets my gaze learn the true meaning of fear; for I am the harbinger of death.",
                                                          "The other day I was talking with my neighbours and they mentioned hearing weird noises.",
                                                          "The legend tells that a long time ago all seawater was fresh.",
                                                          "I’ll have you know I graduated top of my class in the Navy Seals.",
                                                          "According to all known laws of aviation, there is no way that a bee should be able to fly.",
                                                          "The running speed starts slowly, but gets faster each minute after you hear this signal.",
                                                          "Did you ever hear the tragedy of Darth Plagueis The Wise?"]))



                rs3 = smart_truncate(snip_hashtags(s3))
                rs4 = smart_truncate(snip_hashtags(s4))
                rs5 = smart_truncate(snip_hashtags(s5))
                body = ''

                r3 = rs3.split()
                r4 = rs4.split()
                r5 = rs5.split()
                if (frac < 0.3):
                    rl = r3[:math.ceil(len(r3)*(frac + random.choice([0.1,0.15,0.2,0.25,0.3,0.35])))]
                    rl.extend(r4[math.ceil(len(r4)*(frac + random.choice([0,0.05,0.1,0.15,0.2,0.25,0.3,0.35]))):math.ceil(len(r4)*(frac + random.choice([0.4,0.45,0.5,0.55,0.6])))])
                    rl.extend(r5[math.ceil(len(r5)*(frac + random.choice([0.05,0.1,0.15,0.2,0.25,0.3,0.35]))):])
                else:
                    rl = r3[:math.ceil(len(r3)*(frac + random.choice([0.1,0.15,0.2,0.25,0.3,0.35])))]
                    rl.extend(r4[math.ceil(len(r4)*(frac + random.choice([0,0.05,0.1,0.15,0.2,0.25,0.3,0.35]))):math.ceil(len(r4)*(frac + random.choice([0.4,0.45,0.5,0.55,0.6])))])
                    rl.extend(r5[math.ceil(len(r5)*(frac + random.choice([0,0.05,0.1,0.15,0.2,0.25,0.3,0.35]))):math.ceil(len(r5)*(frac + random.choice([0.4,0.45,0.5,0.55,0.6])))])
                    rl.extend(r3[math.ceil(len(r3)*(frac + random.choice([0.1,0.15,0.2,0.25,0.3,0.35]))):])
                body = smart_truncate(" ".join(rl),length=500)

                logger.debug("Generated text (body): %s" % body) # Print the final reply

                try:
                    if toot:
                        logger.debug(f"Posting '{title}'...")
                        result = post(title, body)

                        if not result:
                            logger.error("Post Failed! Attempting to login and post again...")
                            result = login() and post(title, body)
                    
                        if result:
                            logger.info(f"Successfully posted '{title}'")
                        else:
                            logger.error("Failed on retry =/")
                
                except Exception as e:
                    logger.error(f"Got exception while posting link: {e}")
        
            #new_threads = list_threads(KBOT_MAGAZINE, True)

            # comment = "\n".join([part.strip() for part in ("""
            # @rideranton@kbin.social
            # @rideranton@fedia.io

            # Proximal Flame has posted a new chapter in The Last Angel!

            # ---
            # This action was performed automatically.
            # """.strip("\n").split("\n"))]).strip("\n")

            # for thread_id in new_threads:
            #     if thread_id in threads:
            #         continue
            #     post_toplevel_comment(KBOT_MAGAZINE, thread_id, comment)

            if result:
                if os.path.exists(cache_name):
                    shutil.copyfile(cache_name, f"{cache_name}.bak")

                try:
                    with open(cache_name, "w") as f:
                        f.write(datetime.utcnow().replace(tzinfo=tzutc()).isoformat())
                except Exception as e:
                    logger.error(f"Got exception while writing access time: {e}")
                    if os.path.exists(f"{cache_name}.bak"):
                        shutil.copyfile(f"{cache_name}.bak", cache_name)
                finally:
                    if os.path.exists(f"{cache_name}.bak"):
                        os.remove(f"{cache_name}.bak")

            sleep(KBOT_FREQUENCY)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error("Unhandled Error:", e)
            sleep(KBOT_FREQUENCY)

    hal.close()

if __name__ == "__main__":
    main()