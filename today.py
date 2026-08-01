import datetime
from dateutil import relativedelta
import requests
import os
from lxml import etree
import time
import hashlib
from collections import Counter


def load_local_env():
    """Load local .env values without overriding environment variables from GitHub Actions."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    try:
        with open(env_path, encoding='utf-8') as env_file:
            for raw_line in env_file:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


load_local_env()

# Fine-grained personal access token with All Repositories access:
# Account permissions: read:Followers, read:Starring, read:Watching
# Repository permissions: read:Commit statuses, read:Contents, read:Issues, read:Metadata, read:Pull Requests
HEADERS = {'authorization': 'token ' + os.environ.get('ACCESS_TOKEN', '')}
USER_NAME = os.environ.get('USER_NAME', 'zaynedoc')
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0, 'public_repo_languages': 0, 'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def daily_readme(birthday):
    """
    Returns the length of time since I was born
    e.g. 'XX years, XX months, XX days'
    """
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    return '{} {}, {} {}, {} {}{}'.format(
        diff.years, 'year' + format_plural(diff.years), 
        diff.months, 'month' + format_plural(diff.months), 
        diff.days, 'day' + format_plural(diff.days),
        ' 🎂' if (diff.months == 0 and diff.days == 0) else '')


def format_plural(unit):
    """
    Returns a properly formatted number
    e.g.
    'day' + format_plural(diff.days) == 5
    >>> '5 days'
    'day' + format_plural(diff.days) == 1
    >>> '1 day'
    """
    return 's' if unit != 1 else ''


def simple_request(func_name, query, variables):
    """
    Returns a request, or raises an Exception if the response does not succeed.
    """
    request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    if request.status_code == 401:
        raise RuntimeError('GitHub rejected ACCESS_TOKEN. Update the token in .env or your environment variables, then run today.py again.')
    raise Exception(func_name, ' has failed with a', request.status_code, request.text, QUERY_COUNT)


def graph_commits(start_date, end_date):
    """
    Uses GitHub's GraphQL v4 API to return my total commit count
    """
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date,'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def graph_repos_stars(count_type, owner_affiliation, cursor=None, add_loc=0, del_loc=0):
    """
    Uses GitHub's GraphQL v4 API to return my total repository, star, or lines of code count.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers {
                                totalCount
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    if request.status_code == 200:
        if count_type == 'repos':
            return request.json()['data']['user']['repositories']['totalCount']
        elif count_type == 'stars':
            return stars_counter(request.json()['data']['user']['repositories']['edges'])


def public_repo_languages(cursor=None, languages=None):
    """
    Returns language shares across public repositories owned by the user, ordered by size.

    GitHub reports repository language composition as byte counts. Those byte counts
    are summed across repositories, making the percentages code-volume weighted rather
    than one vote per repository. GitHub's API does not provide per-language line counts.
    """
    query_count('public_repo_languages')
    query = '''
    query ($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, privacy: PUBLIC) {
                nodes {
                    languages(first: 100) {
                        edges {
                            size
                            node {
                                name
                            }
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    request = simple_request(public_repo_languages.__name__, query, {'login': USER_NAME, 'cursor': cursor})
    repositories = request.json()['data']['user']['repositories']

    if languages is None:
        languages = Counter()

    for repo in repositories['nodes']:
        for language in repo['languages']['edges']:
            languages[language['node']['name']] += language['size']

    if repositories['pageInfo']['hasNextPage']:
        return public_repo_languages(repositories['pageInfo']['endCursor'], languages)

    total_size = sum(languages.values())
    if total_size == 0:
        return []

    return [
        (language, size, (size / total_size) * 100)
        for language, size in languages.most_common()
    ]


def recursive_loc(owner, repo_name, data, cache_comment, addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    """
    Uses GitHub's GraphQL v4 API and cursor pagination to fetch 100 commits from a repository at a time
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit {
                                        committedDate
                                    }
                                    author {
                                        user {
                                            id
                                        }
                                    }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo {
                                endCursor
                                hasNextPage
                            }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    try:
        request = requests.post('https://api.github.com/graphql', json={'query': query, 'variables':variables}, headers=HEADERS) # I cannot use simple_request(), because I want to save the file before raising Exception
        if request.status_code == 200:
            res_json = request.json()
            if 'data' in res_json and res_json['data'] is not None and res_json['data'].get('repository') is not None and res_json['data']['repository'].get('defaultBranchRef') is not None: # Only count commits if repo isn't empty
                return loc_counter_one_repo(owner, repo_name, data, cache_comment, res_json['data']['repository']['defaultBranchRef']['target']['history'], addition_total, deletion_total, my_commits)
            else: return addition_total, deletion_total, my_commits
        print(f"\nWarning: recursive_loc failed for {owner}/{repo_name} with status code {request.status_code}. Returning partial stats collected so far.")
    except Exception as e:
        print(f"\nWarning: recursive_loc failed for {owner}/{repo_name} with network/API error: {e}. Returning partial stats collected so far.")
    return addition_total, deletion_total, my_commits



def loc_counter_one_repo(owner, repo_name, data, cache_comment, history, addition_total, deletion_total, my_commits):
    """
    Recursively call recursive_loc (since GraphQL can only search 100 commits at a time) 
    only adds the LOC value of commits authored by me
    """
    for node in history['edges']:
        if node['node']['author']['user'] == OWNER_ID:
            my_commits += 1
            addition_total += node['node']['additions']
            deletion_total += node['node']['deletions']

    if history['edges'] == [] or not history['pageInfo']['hasNextPage']:
        return addition_total, deletion_total, my_commits
    else: return recursive_loc(owner, repo_name, data, cache_comment, addition_total, deletion_total, my_commits, history['pageInfo']['endCursor'])


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=[]):
    """
    Uses GitHub's GraphQL v4 API to query all the repositories I have access to (with respect to owner_affiliation)
    Queries 60 repos at a time, because larger queries give a 502 timeout error and smaller queries send too many
    requests and also give a 502 error.
    Returns the total number of lines of code in all repositories
    """
    query_count('loc_query')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
            edges {
                node {
                    ... on Repository {
                        nameWithOwner
                        defaultBranchRef {
                            target {
                                ... on Commit {
                                    history {
                                        totalCount
                                        }
                                    }
                                    }
                                }
                            }
                        }
                    }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(loc_query.__name__, query, variables)
    if request.json()['data']['user']['repositories']['pageInfo']['hasNextPage']:   # If repository data has another page
        edges += request.json()['data']['user']['repositories']['edges']            # Add on to the LoC count
        return loc_query(owner_affiliation, comment_size, force_cache, request.json()['data']['user']['repositories']['pageInfo']['endCursor'], edges)
    else:
        return cache_builder(edges + request.json()['data']['user']['repositories']['edges'], comment_size, force_cache)


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    """
    Checks each repository in edges to see if it has been updated since the last time it was cached
    If it has, run recursive_loc on that repository to update the LOC count
    """
    cached = True # Assume all repositories are cached
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Create a unique filename for each user
    try:
        with open(filename, 'r') as f:
            data = f.readlines()
    except FileNotFoundError: # If the cache file doesn't exist, create it
        data = []
        if comment_size > 0:
            for _ in range(comment_size): data.append('This line is a comment block. Write whatever you want here.\n')
        with open(filename, 'w') as f:
            f.writelines(data)

    if len(data)-comment_size != len(edges) or force_cache: # If the number of repos has changed, or force_cache is True
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, 'r') as f:
            data = f.readlines()

    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]['node']['nameWithOwner'].encode('utf-8')).hexdigest():
            try:
                if int(commit_count) != edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']:
                    # if commit count has changed, update loc for that repo
                    owner, repo_name = edges[index]['node']['nameWithOwner'].split('/')
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = repo_hash + ' ' + str(edges[index]['node']['defaultBranchRef']['target']['history']['totalCount']) + ' ' + str(loc[2]) + ' ' + str(loc[0]) + ' ' + str(loc[1]) + '\n'
            except TypeError: # If the repo is empty
                data[index] = repo_hash + ' 0 0 0 0\n'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    """
    Wipes the cache file
    This is called when the number of repositories changes or when the file is first created
    """
    with open(filename, 'r') as f:
        data = []
        if comment_size > 0:
            data = f.readlines()[:comment_size] # only save the comment
    with open(filename, 'w') as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node['node']['nameWithOwner'].encode('utf-8')).hexdigest() + ' 0 0 0 0\n')


def add_archive():
    """
    Several repositories I have contributed to have since been deleted.
    This function adds them using their last known data
    """
    with open('cache/repository_archive.txt', 'r') as f:
        data = f.readlines()
    old_data = data
    data = data[7:len(data)-3] # remove the comment block    
    added_loc, deleted_loc, added_commits = 0, 0, 0
    contributed_repos = len(data)
    for line in data:
        repo_hash, total_commits, my_commits, *loc = line.split()
        added_loc += int(loc[0])
        deleted_loc += int(loc[1])
        if (my_commits.isdigit()): added_commits += int(my_commits)
    added_commits += int(old_data[-1].split()[4][:-1])
    return [added_loc, deleted_loc, added_loc - deleted_loc, added_commits, contributed_repos]


def force_close_file(data, cache_comment):
    """
    Forces the file to close, preserving whatever data was written to it
    This is needed because if this function is called, the program would've crashed before the file is properly saved and closed
    """
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt'
    with open(filename, 'w') as f:
        f.writelines(cache_comment)
        f.writelines(data)
    print('There was an error while writing to the cache file. The file,', filename, 'has had the partial data saved and closed.')


def stars_counter(data):
    """
    Count total stars in repositories owned by me
    """
    total_stars = 0
    for node in data: total_stars += node['node']['stargazers']['totalCount']
    return total_stars


def svg_overwrite(filename, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, loc_data, language_data):
    """
    Parse SVG files and update elements with my age, commits, stars, repositories, and lines written
    """
    tree = etree.parse(filename)
    root = tree.getroot()
    
    # Format integers to strings with commas
    repo_str = f"{repo_data:,}"
    contrib_str = f"{contrib_data:,}"
    star_str = f"{star_data:,}"
    commit_str = f"{commit_data:,}"
    follower_str = f"{follower_data:,}"
    loc_str = str(loc_data[2])
    loc_add_str = str(loc_data[0])
    loc_del_str = str(loc_data[1])
    
    # Calculate lengths of variable parts
    uptime_var = len(age_data)
    line1_var = len(repo_str) + len(contrib_str) + len(star_str)
    line2_var = len(commit_str) + len(follower_str)
    line3_var = len(loc_str) + len(loc_add_str) + len(loc_del_str)
    
    # Calculate target width (max_W) dynamically (58 is target width excluding leading '. ')
    max_W = max(58, 9 + uptime_var, 48 + len(star_str), 51 + len(follower_str), 27 + line3_var)
    
    # 1. Uptime Line (Key "Uptime:" has 7 chars. Dots/value spaces takes 2 chars. So target offset is max_W - 9)
    justify_format(root, 'age_data', age_data, max_W - 9)
    
    # 2. Line 1: Repos & Stars (Dynamic pipe alignment at col 35)
    repo_dots_len = 10 - len(repo_str) - len(contrib_str)
    if repo_dots_len <= 2:
        repo_dots = ' ' if repo_dots_len <= 0 else '. '
    else:
        repo_dots = ' ' + ('.' * repo_dots_len) + ' '
        
    left_1_len = 34 # constant length before " | Stars:" space
    star_dots_len = max_W - left_1_len - 1 - 8 - len(star_str) - 2
    if star_dots_len <= 2:
        star_dots = ' ' if star_dots_len <= 0 else '. '
    else:
        star_dots = ' ' + ('.' * star_dots_len) + ' '
        
    find_and_replace(root, 'repo_data_dots', repo_dots)
    find_and_replace(root, 'repo_data', repo_str)
    find_and_replace(root, 'contrib_data', contrib_str)
    find_and_replace(root, 'star_data_dots', star_dots)
    find_and_replace(root, 'star_data', star_str)
    
    # 3. Line 2: Commits & Followers (Dynamic pipe alignment at col 35)
    commit_dots_len = 23 - len(commit_str)
    if commit_dots_len <= 2:
        commit_dots = ' ' if commit_dots_len <= 0 else '. '
    else:
        commit_dots = ' ' + ('.' * commit_dots_len) + ' '
        
    left_2_len = 34 # constant length before " | Followers:" space
    follower_dots_len = max_W - left_2_len - 1 - 12 - len(follower_str) - 2
    if follower_dots_len <= 2:
        follower_dots = ' ' if follower_dots_len <= 0 else '. '
    else:
        follower_dots = ' ' + ('.' * follower_dots_len) + ' '
        
    find_and_replace(root, 'commit_data_dots', commit_dots)
    find_and_replace(root, 'commit_data', commit_str)
    find_and_replace(root, 'follower_data_dots', follower_dots)
    find_and_replace(root, 'follower_data', follower_str)
    
    # 4. Line 3: Lines of Code (Key "Lines of Code:" has 14 chars. Paren syntax takes 9 chars. So left offset constant is 23)
    left_3_len = 23 + len(loc_str) + len(loc_add_str) + len(loc_del_str)
    loc_dots_len = max_W - left_3_len - 2
    if loc_dots_len <= 2:
        loc_dots = ' ' if loc_dots_len <= 0 else '. '
    else:
        loc_dots = ' ' + ('.' * loc_dots_len) + ' '
        
    find_and_replace(root, 'loc_data_dots', loc_dots)
    find_and_replace(root, 'loc_data', loc_str)
    find_and_replace(root, 'loc_add', loc_add_str)
    find_and_replace(root, 'loc_del', loc_del_str)

    # 5. Public-repository language card: show the largest eight languages.
    for index in range(8):
        if index < len(language_data):
            language, _size, percentage = language_data[index]
            language_line = f'{language[:17]:<11} {percentage:>5.1f}%'
        else:
            language_line = ''
        find_and_replace(root, f'language_{index + 1}', language_line)

    # 6. Fit the remaining language names into the 39-character Other Exp. panel.
    other_prefix = '| Other:'
    other_panel_width = 39
    other_value_width = other_panel_width - len(other_prefix) - 3  # minimum " . " filler
    other_names = []
    for language, _size, _percentage in language_data[8:]:
        candidate = ', '.join(other_names + [language])
        if len(candidate) > other_value_width:
            break
        other_names.append(language)

    other_value = ', '.join(other_names) if other_names else 'None'
    other_dot_count = max(1, other_panel_width - len(other_prefix) - len(other_value) - 2)
    find_and_replace(root, 'other_languages', other_value)
    find_and_replace(root, 'other_languages_dots', ' ' + ('.' * other_dot_count) + ' ')
    
    tree.write(filename, encoding='utf-8', xml_declaration=True)


def justify_format(root, element_id, new_text, length=0):
    """
    Updates and formats the text of the element, and modifes the amount of dots in the previous element to justify the new text on the svg
    """
    if isinstance(new_text, int):
        new_text = f"{'{:,}'.format(new_text)}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def find_and_replace(root, element_id, new_text):
    """
    Finds every matching element in the SVG file and replaces its text with a new value.
    """
    for element in root.findall(f".//*[@id='{element_id}']"):
        element.text = new_text


def commit_counter(comment_size):
    """
    Counts up my total commits, using the cache file created by cache_builder.
    """
    total_commits = 0
    filename = 'cache/'+hashlib.sha256(USER_NAME.encode('utf-8')).hexdigest()+'.txt' # Use the same filename as cache_builder
    with open(filename, 'r') as f:
        data = f.readlines()
    cache_comment = data[:comment_size] # save the comment block
    data = data[comment_size:] # remove those lines
    for line in data:
        total_commits += int(line.split()[2])
    return total_commits


def user_getter(username):
    """
    Returns the account ID and creation time of the user
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    """
    Returns the number of followers of the user
    """
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def query_count(funct_id):
    """
    Counts how many times the GitHub GraphQL API is called
    """
    global QUERY_COUNT
    QUERY_COUNT[funct_id] += 1


def perf_counter(funct, *args):
    """
    Calculates the time it takes for a function to run
    Returns the function result and the time differential
    """
    start = time.perf_counter()
    funct_return = funct(*args)
    return funct_return, time.perf_counter() - start


def formatter(query_type, difference, funct_return=False, whitespace=0):
    """
    Prints a formatted time differential
    Returns formatted result if whitespace is specified, otherwise returns raw result
    """
    print('{:<23}'.format('   ' + query_type + ':'), sep='', end='')
    print('{:>12}'.format('%.4f' % difference + ' s ')) if difference > 1 else print('{:>12}'.format('%.4f' % (difference * 1000) + ' ms'))
    if whitespace:
        return f"{'{:,}'.format(funct_return): <{whitespace}}"
    return funct_return


if __name__ == '__main__':
    """
    Zayne Dockery (zaynedoc), 2026
    """
    print('Calculation times:')
    # define global variable for owner ID and calculate user's creation date
    user_data, user_time = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    formatter('account data', user_time)
    
    # Using November 18, 2005 as the exact birthday for Uptime calculations
    age_data, age_time = perf_counter(daily_readme, datetime.datetime(2005, 11, 18))
    formatter('age calculation', age_time)
    
    total_loc, loc_time = perf_counter(loc_query, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], 7)
    formatter('LOC (cached)', loc_time) if total_loc[-1] else formatter('LOC (no cache)', loc_time)
    
    commit_data, commit_time = perf_counter(commit_counter, 7)
    star_data, star_time = perf_counter(graph_repos_stars, 'stars', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    repo_data, repo_time = perf_counter(graph_repos_stars, 'repos', ['OWNER'])
    contrib_data, contrib_time = perf_counter(graph_repos_stars, 'repos', ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    language_data, language_time = perf_counter(public_repo_languages)
    formatter('public repo languages', language_time)

    # If the user has archived contributions (optional)
    if OWNER_ID == {'id': 'MDQ6VXNlcjQ5MjUyNDA3'}: # only calculate for user zaynedoc
        try:
            archived_data = add_archive()
            for index in range(len(total_loc)-1):
                total_loc[index] += archived_data[index]
            contrib_data += archived_data[-1]
            commit_data += int(archived_data[-2])
        except FileNotFoundError:
            pass

    for index in range(len(total_loc)-1): total_loc[index] = '{:,}'.format(total_loc[index]) # format added, deleted, and total LOC

    svg_overwrite('dark_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], language_data)
    svg_overwrite('light_mode.svg', age_data, commit_data, star_data, repo_data, contrib_data, follower_data, total_loc[:-1], language_data)

    # move cursor to override 'Calculation times:' with 'Total function time:' and the total function time, then move cursor back
    print('\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F\033[F',
        '{:<21}'.format('Total function time:'), '{:>11}'.format('%.4f' % (user_time + age_time + loc_time + commit_time + star_time + repo_time + contrib_time + follower_time + language_time)),
        ' s \033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E\033[E', sep='')

    print('Total GitHub GraphQL API calls:', '{:>3}'.format(sum(QUERY_COUNT.values())))
    for funct_name, count in QUERY_COUNT.items(): print('{:<28}'.format('   ' + funct_name + ':'), '{:>6}'.format(count))
