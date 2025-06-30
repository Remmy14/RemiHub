# Python Imports
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
import os
from random import randint
import re
import shutil
import sys
import time

# 3rd Party Imports
from plexapi.server import PlexServer

# Local Imports
sys.path.append('M:/Q_Drive/Projects/RemiHub/')
from backend.config import load_config


# ----------------------
# Configure Logging
# ----------------------
logger = logging.getLogger('PlexMonitor')
logger.setLevel(logging.INFO)

log_handler = RotatingFileHandler('backend/logs/plex_dl_monitor.log', maxBytes=1_000_000, backupCount=3)
formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s')
log_handler.setFormatter(formatter)
logger.addHandler(log_handler)


def print(message):
    # Override the built in Print so that I don't have to change everything
    now = datetime.now()
    __builtins__.print(f'{now} - {message}')

def get_new_downloads(library):
    # Check for a "maintenance" flag
    if os.path.exists(f'C:/Users/alexr/Documents/Temp/maintenance.flg'):
        return False
    files = os.listdir(library)
    ignores = ['encrypted']

    # Keep track of new files
    new_files = False

    for file in files:
        enc_file = f'{library}/{file}.encrypted'
        if any(file.endswith(s) for s in ['part', 'encrypted', 'rar', 'zip']):
            continue
        elif os.path.exists(enc_file):
            # The file is still being decrypted
            continue
        elif os.path.isdir(os.path.join(library, file)):
            # We are in a folder, process that folder first
            sub_dir = f'{library}/{file}/'
            get_new_downloads(sub_dir)
            # Next, check if it's empty and delete it
            lingerers = os.listdir(sub_dir)
            if len(lingerers) == 0:
                logger.info(f'Subdir {sub_dir} is empty, removing tree')
                try:
                    shutil.rmtree(sub_dir)
                except Exception as e:
                    logger.error(f'Error: {e}')

            # This was a directory, move on to the next
            continue

        # We have a new download that needs processed
        src_path = os.path.join(f'{library}/{file}')
        cur_library = library.split('/')[5]

        try:
            if cur_library == 'TV':
                process_tv(src_path)
                new_files = True
            elif cur_library == 'Movies':
                process_movie(src_path)
                new_files = True
            else:
                logger.error(f'Illegal library: {cur_library}')
        except Exception as e:
            logger.error(f'Error Processing Item: {e}')
            quarantine_item(src_path)

    # Return whether or not we had files to move
    return new_files

def process_movie(item_path):
    new_item = item_path.split('/')[-1].upper()
    try:
        year = re.search('\\d{4}', new_item).group()
    except:
        logger.info(f'No year found for {new_item}')
        return

    # Get the file extension and save it for later
    # extension = new_item[-3:].lower()
    extension = os.path.splitext(item_path)[1].lstrip('.').lower()

    name = new_item.split(year)[0].replace('.', ' ').strip()
    new_name = f'{name.title()} ({year}).{extension}'
    move_file(item_path, new_name, 'Movies')

def process_tv(item_path):
    # We need to get the series name
    # We will split on the season/episode number, hopefully it's in a standard format
    new_item = item_path.split('/')[-1].upper()
    new_item = new_item.replace('.', ' ')
    info = re.search('S\\d{1,2}([-+]?E\\d{1,2})+', new_item)

    if not info:
        try:
            info = re.search('EP\\d{1,2}', new_item)

            info = info.group()
            season = 1
            episode = int(info.strip('EP'))
        except:
            info = None

        if not info:
            try:
                info = re.search('\\d{1}X\\d{1,2}', new_item)
                info = info.group()
                season = int(info.split('X')[0])
                episode = int(info.split('X')[1])
            except:
                info = None
    else:
        info = info.group()
        season = int(info.split('E')[0].split('S')[1])
        episode = int(info.split('E')[1])
    series = new_item.split(info)[0].strip().title()
    extra = new_item.split(info)[1].replace('-', '').replace('(', '').replace(')', '')

    # Special cases on Names
    if series.upper() == 'MXC':
        series = 'MXC Most Extreme Elimination Challenge'

    # Special Cases:
    if ' Ii ' in series:
        series = series.replace(' Ii ', ' II ')
    elif ' Iii ' in series:
        series = series.replace(' Iii ', ' Iii ')
    if 'Bbc' in series:
        series = series.replace('Bbc', 'BBC')

    # Get the file extension and save it for late
    extension = new_item[-3:].lower()

    # Find the resolution if there is one
    try:
        res = re.search('\\d{3,4}P', extra).group()
    except Exception as e:
        #print(f'E: {e}')
        res = None

    # Grab everything before the resolution
    if res:
        ep_name = extra.split(res)[0].strip().title()
        ep_name = f' - {ep_name}'
    else:
        ep_name = ''

    # Now that we have our bits, start processing
    try:
        #year = re.search('\\d{2,4}', series).group()
        year = re.search('\\d{4}', series).group()
    except:
        year = None

    if year:
        series = series.split(year)[0].replace('(', '').replace(')', '').strip()

        new_name = f'{series} ({year}) - S{season:02d}E{episode:02d}{ep_name}.{extension}'
    else:
        new_name = f'{series} - S{season:02d}E{episode:02d}{ep_name}.{extension}'
    move_file(item_path, new_name,'TV', series=series, season=season)

def quarantine_item(item_path):
    '''
    Will quarantine an item that has caused the naming convention to fail for any reason
    :param item_path:
    :return:
    '''
    logger.info(f'Quarantining Item: {item_path}')
    try:
        dest = 'C:/Users/alexr/Documents/Temp/Quarantined/'

        if not os.path.exists(dest):
            os.makedirs(dest)

        shutil.move(item_path, dest)
    except Exception as e:
        logger.error(f'Error on Quarantine Item: {e}')

def move_file(source, new_name, library, series=None, season=None):
    # If we are running a TV show, we need to check if the show exists on a drive somewhere
    drives = ['G:/', ]

    if library == 'TV' and series:
        # We need to do a search for this series
        dest = None
        for drive in drives:
            existing_dirs  = os.listdir(drive + 'TV/')
            for dir in existing_dirs:
                if series in dir:
                    # Possible match
                    # TODO: Enhance!
                    dest  = f'{drive}{library}/{dir}/'
                    break
        if not dest:
            # No pre-existing destination exists, we need to create one
            dest = f'{drives[randint(0, len(drives) - 1)]}{library}/{new_name.split('-')[0].strip()}'
            logger.info(f'Creating new directory: {dest}')

        dest = os.path.join(dest, f'Season {season:02d}')
        if os.path.exists(dest):
            pass
        else:
            logger.info(f'No Season Dir Found for {dest}')
            os.makedirs(dest)
    # Process Movies:
    elif library == 'Movies':
        dest = f'{drives[randint(0, len(drives) - 1)]}/Movies/'

    # Append our pre-calculated new name to the destination directory
    dest = f'{dest}/{new_name}'
    logger.info(f'Moving {source} ==> {dest}')
    try:
        shutil.move(source, dest)
    except:
        pass


def main():
    logger.info(f'Starting Plex DL Monitor Service')

    # Initialize our config
    config = load_config('config/config.ini')['Plex Monitor']

    download_dir = config['download_dir']
    libraries = ['TV', 'Movies', ]

    # Get an instance of our Plex server
    baseurl = config['baseurl']
    PlexToken = config['plextoken']
    plex = PlexServer(baseurl, PlexToken)

    # Get an object of each library
    tv = plex.library.section('TV Shows')
    movies = plex.library.section('Movies')

    while(True):
        for library in libraries:
            new_files = get_new_downloads(download_dir + library)

            # If we had new files, we need to scan the library
            if new_files:
                if library == 'TV':
                    tv.update()
                elif library == 'Movies':
                    movies.update()

        time.sleep(30)


if __name__ == '__main__':
    main()
    #process_tv('The White Princess (2017) - S01E07 - Two Kings (1080p BluRay x265 RCVR).mkv')
