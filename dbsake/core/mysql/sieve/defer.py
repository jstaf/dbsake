"""
dbsake.core.mysql.sieve.defer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Support for deferring indexes and constraints
"""
import csv
import io
import logging
import re

debug = logging.debug
warn = logging.warn


def extract_create_table(section):
    result = []
    for line in section.iterable:
        if line.startswith(b'CREATE TABLE'):
            result.append(line)
        elif result:
            result.append(line)
            if line.rstrip().endswith(b';'):
                break
    return b''.join(result)


KEY_CRE = re.compile(br'\s*(?:UNIQUE )?KEY (?P<name>`.+`) \((?P<columns>.+)\)'
                     br'(?: USING (?:BTREE|HASH))?,?$')


def extract_indexes(table_ddl):
    result = []
    for line in table_ddl.splitlines(True):
        match = KEY_CRE.match(line)
        if not match:
            continue
        result.append((parse_columns(match.group('name'))[0],
                       parse_columns(match.group('columns')),
                       line))
    return result


CONSTRAINT_CRE = re.compile(br'^\s*CONSTRAINT (?P<name>`.+`) FOREIGN KEY '
                            br'\((?P<columns>.+)\) REFERENCES')


def extract_constraints(table_ddl):
    result = []
    for line in table_ddl.splitlines(True):
        match = CONSTRAINT_CRE.match(line)
        if not match:
            continue
        result.append((parse_columns(match.group('name'))[0],
                       parse_columns(match.group('columns')),
                       line))
    return result


def parse_columns(value):
    value = value.decode('utf8')
    reader = csv.reader(io.StringIO(value),
                        quotechar='`',
                        skipinitialspace=True)
    return tuple(column.encode('utf8') for row in reader for column in row)


IDENT_CRE = re.compile(br'CREATE TABLE .*`(?P<name>.+)` \($')


def extract_table_name(table_ddl):
    for line in table_ddl.splitlines():
        match = IDENT_CRE.match(line)
        if match:
            return match.group('name')
    raise ValueError("Failed to find table name from DDL: %s" % table_ddl)


def format_alter_table(table_ddl, indexes):
    table = extract_table_name(table_ddl)
    lines = [line.strip() for _, _, line in indexes]
    if not lines:
        return ""
    template = [
        b'--',
        b'-- InnoDB Fast Index Creation (generated by dbsake)',
        b'--',
        b'',
        b'ALTER TABLE `' + table + b'`',
        b'  ADD ' + b'\n  ADD '.join(lines)
    ]
    ddl = b'\n'.join(template)
    return ddl.rstrip(b',') + b';'


def format_create_table(table_ddl, indexes):
    result = []
    deferred_lines = set(line for _, _, line in indexes)
    # this formatting logic intends to strip trailing commas on lines just
    # before closing parenthese to ensure valid SQL sytax, even if some
    # lines have been pruned due to index/fk deferal
    for line in table_ddl.splitlines(True):
        # strip traiing comma on line just before ') ... ENGINE=...'
        if result and line.startswith(b')'):
            result[-1] = result[-1].rstrip().rstrip(b",") + b'\n'
        if line not in deferred_lines:
            result.append(line)
    return b''.join(result)


def split_indexes(section, defer_constraints=False):
    """Remove indexes from a Table structure section


    :returns: deferred constraints
    """
    # ensure iterable can be iterated multiple times
    section.iterable = list(section.iterable)
    table_ddl = extract_create_table(section)
    # Only defer indexes for InnoDB
    if b'ENGINE=InnoDB' not in table_ddl:
        debug("%s.%s is not an innodb table. Skipping index rewrite.",
              section.database, section.table)
        return b''
    constraints = []
    indexes = extract_indexes(table_ddl)
    constraints = extract_constraints(table_ddl)
    if not defer_constraints:
        preserved_indexes = set()
        for _name, _columns, _ in constraints:
            # read the indexes in sorted order - sorted by number of columns
            # flag the first matching index we find and move on to the next
            # constraint.
            for name, columns, line in sorted(indexes,
                                              key=lambda x: len(x[1])):
                # if this index is a prefix to the constraint
                if columns[0:len(_columns)] == _columns:
                    preserved_indexes.add((name, columns, line, _name))
                    break

        for index_name, columns, line, constraint in preserved_indexes:
            warn("Not deferring %s.%s index `%s` - used by constraint `%s`",
                 section.database, section.table, index_name, constraint)
            indexes.remove((index_name, columns, line))
    else:
        indexes += constraints

    patched_create_table = format_create_table(table_ddl, indexes)
    section_rewrite = b''.join(section.iterable)
    section_rewrite = section_rewrite.replace(table_ddl, patched_create_table)
    section.iterable = section_rewrite.splitlines(True)
    return format_alter_table(table_ddl, indexes)
