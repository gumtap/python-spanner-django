# Copyright 2020 Google LLC
#
# Use of this source code is governed by a BSD-style
# license that can be found in the LICENSE file or at
# https://developers.google.com/open-source/licenses/bsd

import google.api_core.exceptions as grpc_exceptions

from .exceptions import (
    Error, IntegrityError, OperationalError, ProgrammingError,
)
from .parse_utils import (
    STMT_DDL, STMT_INSERT, STMT_NON_UPDATING, classify_stmt,
    ensure_where_clause, get_param_types, parse_insert,
    sql_pyformat_args_to_spanner,
)

_UNSET_COUNT = -1


class Cursor(object):
    def __init__(self, db_handle):
        self.__itr = None
        self.__res = None
        self.__row_count = _UNSET_COUNT
        self.__connection = db_handle
        self.__last_op = None
        self.__closed = False

        # arraysize is a readable and writable property mandated
        # by PEP-0249 https://www.python.org/dev/peps/pep-0249/#arraysize
        # It determines the results of .fetchmany
        self.arraysize = 1

    @property
    def description(self):
        if not (self.__res and self.__res.metadata):
            return None

        row_type = self.__res.metadata.row_type
        columns = []
        for field in row_type.fields:
            columns.append(Column(name=field.name, type_code=field.type.code))
        return tuple(columns)

    @property
    def rowcount(self):
        return self.__row_count

    def __raise_if_already_closed(self):
        """
        Raises an exception if attempting to use an already closed connection.
        """
        if self.__closed:
            raise Error('attempting to use an already closed connection')

    def close(self):
        self.__clear()
        self.__closed = True

    def __get_txn(self):
        return self.__connection.get_txn()

    def execute(self, sql, args=None):
        """
        Abstracts and implements execute SQL statements on Cloud Spanner.

        Args:
            sql: A SQL statement
            *args: variadic argument list
            **kwargs: key worded arguments

        Returns:
            None
        """
        self.__raise_if_already_closed()

        if not self.__connection:
            raise ProgrammingError('Cursor is not connected to the database')

        self.__res = None

        # Classify whether this is a read-only SQL statement.
        try:
            classification = classify_stmt(sql)
            if classification == STMT_DDL:
                self.__connection.append_ddl_statement(sql)
                return

            # For every other operation, we've got to ensure that
            # any prior DDL statements were run.
            self.__run_prior_DDL_statements()

            if classification == STMT_NON_UPDATING:
                self.__handle_DQL(self.__get_txn(), sql, args or None)
            elif classification == STMT_INSERT:
                self.__handle_insert(self.__get_txn(), sql, args or None)
            else:
                self.__handle_update(self.__get_txn(), sql, args or None)
        except (grpc_exceptions.AlreadyExists, grpc_exceptions.FailedPrecondition) as e:
            raise IntegrityError(e.details if hasattr(e, 'details') else e)
        except grpc_exceptions.InvalidArgument as e:
            raise ProgrammingError(e.details if hasattr(e, 'details') else e)
        except grpc_exceptions.InternalServerError as e:
            raise OperationalError(e.details if hasattr(e, 'details') else e)

    def __handle_update(self, txn, sql, params, param_types=None):
        sql = ensure_where_clause(sql)
        sql, params = sql_pyformat_args_to_spanner(sql, params)

        res = txn.execute_update(sql, params=params, param_types=get_param_types(params))
        self.__itr = None
        if type(res) == int:
            self.__row_count = res

        return res

    def __handle_insert(self, txn, sql, params):
        parts = parse_insert(sql, params)

        # The split between the two styles exists because:
        # in the common case of multiple values being passed
        # with simple pyformat arguments,
        #   SQL: INSERT INTO T (f1, f2) VALUES (%s, %s, %s)
        #   Params:   [(1, 2, 3, 4, 5, 6, 7, 8, 9, 10,)]
        # we can take advantage of a single RPC with:
        #       transaction.insert(table, columns, values)
        # instead of invoking:
        #   with transaction:
        #       for sql, params in sql_params_list:
        #           transaction.execute_sql(sql, params, param_types)
        # which invokes more RPCs and is more costly.

        sql_params_list = parts.get('sql_params_list')
        for sql, params in sql_params_list:
            sql, params = sql_pyformat_args_to_spanner(sql, params)
            param_types = get_param_types(params)
            res = txn.execute_update(sql, params=params, param_types=param_types)
            # TODO: File a bug with Cloud Spanner and the Python client maintainers
            # about a lost commit when res isn't read from.
            if hasattr(res, '__iter__'):
                _ = list(res)
            elif isinstance(res, int):
                self.__row_count = res

    def __do_execute_insert_homogenous(self, transaction, parts):
        # Perform an insert in one shot.
        table = parts.get('table')
        columns = parts.get('columns')
        values = parts.get('values')
        return transaction.insert(table, columns, values)

    def __handle_DQL(self, txn, sql, params):
        # Reference
        #  https://googleapis.dev/python/spanner/latest/session-api.html#google.cloud.spanner_v1.session.Session.execute_sql
        sql, params = sql_pyformat_args_to_spanner(sql, params)
        res = txn.execute_sql(sql, params=params, param_types=get_param_types(params))
        if type(res) == int:
            self.__row_count = res
            self.__itr = None
        else:
            # Immediately using:
            #   iter(response)
            # here, because this Spanner API doesn't provide
            # easy mechanisms to detect when only a single item
            # is returned or many, yet mixing results that
            # are for .fetchone() with those that would result in
            # many items returns a RuntimeError if .fetchone() is
            # invoked and vice versa.
            self.__res = res
            # Read the first element so that StreamedResult can
            # return the metadata after a DQL statement. See issue #155.
            self.__itr = PeekIterator(self.__res)
            # Unfortunately, Spanner doesn't seem to send back
            # information about the number of rows available.
            self.__row_count = _UNSET_COUNT

    def __enter__(self):
        return self

    def __exit__(self, etype, value, traceback):
        if not etype:  # Not an exception thus we should commit.
            self.__connection.commit()
        else:  # An exception occured within the context so rollback.
            self.__connection.rollback()

        self.__clear()

    def __clear(self):
        self.__connection = None
        self.__txn = None

    def executemany(self, operation, seq_of_params):
        if not self.__connection:
            raise ProgrammingError('Cursor is not connected to the database')

        raise ProgrammingError('Unimplemented')

    def __next__(self):
        if self.__itr is None:
            raise ProgrammingError('no results to return')
        return next(self.__itr)

    def __iter__(self):
        if self.__itr is None:
            raise ProgrammingError('no results to return')
        return self.__itr

    def fetchone(self):
        self.__raise_if_already_closed()

        try:
            return next(self)
        except StopIteration:
            return None

    def fetchall(self):
        self.__raise_if_already_closed()

        return list(self.__iter__())

    def fetchmany(self, size=None):
        """
        Fetch the next set of rows of a query result, returning a sequence of sequences.
        An empty sequence is returned when no more rows are available.

        Args:
            size: optional integer to determine the maximum number of results to fetch.


        Raises:
            Error if the previous call to .execute*() did not produce any result set
            or if no call was issued yet.
        """
        self.__raise_if_already_closed()

        if size is None:
            size = self.arraysize

        items = []
        for i in range(size):
            try:
                items.append(tuple(self.__next__()))
            except StopIteration:
                break

        return items

    @property
    def lastrowid(self):
        return None

    def setinputsizes(sizes):
        raise ProgrammingError('Unimplemented')

    def setoutputsize(size, column=None):
        raise ProgrammingError('Unimplemented')

    def __run_prior_DDL_statements(self):
        return self.__connection.run_prior_DDL_statements()

    def list_tables(self):
        # We CANNOT list tables with
        #   SELECT
        #     t.table_name
        #   FROM
        #     information_schema.tables AS t
        #   WHERE
        #     t.table_catalog = '' and t.table_schema = ''
        # with a transaction otherwise we get back:
        #   400 Unsupported concurrency mode in query using INFORMATION_SCHEMA.
        # hence this specialized method.
        return self.__connection.list_tables()


class Column:
    def __init__(self, name, type_code, display_size=None, internal_size=None,
                 precision=None, scale=None, null_ok=False):
        self.name = name
        self.type_code = type_code
        self.display_size = display_size
        self.internal_size = internal_size
        self.precision = precision
        self.scale = scale
        self.null_ok = null_ok

    def __repr__(self):
        return self.__str__()

    def __getitem__(self, index):
        if index == 0:
            return self.name
        elif index == 1:
            return self.type_code
        elif index == 2:
            return self.display_size
        elif index == 3:
            return self.internal_size
        elif index == 4:
            return self.precision
        elif index == 5:
            return self.scale
        elif index == 6:
            return self.null_ok

    def __str__(self):
        rstr = ', '.join([field for field in [
            "name='%s'" % self.name,
            "type_code=%d" % self.type_code,
            None if not self.display_size else "display_size='%s'" % self.display_size,
            None if not self.internal_size else "internal_size='%s'" % self.internal_size,
            None if not self.precision else "precision='%s'" % self.precision,
            None if not self.scale else "scale='%s'" % self.scale,
            None if not self.null_ok else "null_ok='%s'" % self.null_ok,
        ] if field])

        return 'Column(%s)' % rstr


class PeekIterator(object):
    """
    PeekIterator peeks at the first element out of an iterator
    for the sake of operations like auto-population of fields on reading
    the first element.
    """
    def __init__(self, source):
        itr_src = iter(source)

        self.__iters = []
        self.__index = 0

        try:
            head = next(itr_src)
            # Restitch and prepare to read from multiple iterators.
            self.__iters = [iter(itr) for itr in [[head], itr_src]]
        except StopIteration:
            pass

    def __next__(self):
        if self.__index >= len(self.__iters):
            raise StopIteration

        iterator = self.__iters[self.__index]
        try:
            head = next(iterator)
        except StopIteration:
            # That iterator has been exhausted, try with the next one.
            self.__index += 1
            return self.__next__()
        else:
            return head

    def __iter__(self):
        return self
