"""
Base and utility classes for tseries type pandas objects.
"""
from datetime import datetime
from typing import Any, List, Optional, TypeVar, Union, cast

import numpy as np

from pandas._libs import NaT, Timedelta, iNaT, join as libjoin, lib
from pandas._libs.tslibs import BaseOffset, Resolution, Tick, timezones
from pandas._libs.tslibs.parsing import DateParseError
from pandas._typing import Callable, Label
from pandas.compat.numpy import function as nv
from pandas.errors import AbstractMethodError
from pandas.util._decorators import Appender, cache_readonly, doc

from pandas.core.dtypes.common import (
    ensure_int64,
    is_bool_dtype,
    is_dtype_equal,
    is_integer,
    is_list_like,
    is_period_dtype,
    is_scalar,
)
from pandas.core.dtypes.concat import concat_compat
from pandas.core.dtypes.generic import ABCIndex, ABCSeries

from pandas.core import algorithms
from pandas.core.arrays import DatetimeArray, PeriodArray, TimedeltaArray
from pandas.core.arrays.datetimelike import DatetimeLikeArrayMixin
from pandas.core.base import IndexOpsMixin
import pandas.core.common as com
from pandas.core.construction import array as pd_array, extract_array
import pandas.core.indexes.base as ibase
from pandas.core.indexes.base import Index, _index_shared_docs
from pandas.core.indexes.extension import (
    ExtensionIndex,
    inherit_names,
    make_wrapped_arith_op,
)
from pandas.core.indexes.numeric import Int64Index
from pandas.core.ops import get_op_result_name
from pandas.core.sorting import ensure_key_mapped
from pandas.core.tools.timedeltas import to_timedelta

_index_doc_kwargs = dict(ibase._index_doc_kwargs)

_T = TypeVar("_T", bound="DatetimeIndexOpsMixin")


def _join_i8_wrapper(joinf, with_indexers: bool = True):
    """
    Create the join wrapper methods.
    """

    # error: 'staticmethod' used with a non-method
    @staticmethod  # type: ignore[misc]
    def wrapper(left, right):
        if isinstance(left, (np.ndarray, ABCIndex, ABCSeries, DatetimeLikeArrayMixin)):
            left = left.view("i8")
        if isinstance(right, (np.ndarray, ABCIndex, ABCSeries, DatetimeLikeArrayMixin)):
            right = right.view("i8")

        results = joinf(left, right)
        if with_indexers:
            # dtype should be timedelta64[ns] for TimedeltaIndex
            #  and datetime64[ns] for DatetimeIndex
            dtype = left.dtype.base

            join_index, left_indexer, right_indexer = results
            join_index = join_index.view(dtype)
            return join_index, left_indexer, right_indexer
        return results

    return wrapper


@inherit_names(
    ["inferred_freq", "_isnan", "_resolution_obj", "resolution"],
    DatetimeLikeArrayMixin,
    cache=True,
)
@inherit_names(
    ["mean", "asi8", "freq", "freqstr", "_box_func"], DatetimeLikeArrayMixin,
)
class DatetimeIndexOpsMixin(ExtensionIndex):
    """
    Common ops mixin to support a unified interface datetimelike Index.
    """

    _data: Union[DatetimeArray, TimedeltaArray, PeriodArray]
    freq: Optional[BaseOffset]
    freqstr: Optional[str]
    _resolution_obj: Resolution
    _bool_ops: List[str] = []
    _field_ops: List[str] = []

    # error: "Callable[[Any], Any]" has no attribute "fget"
    hasnans = cache_readonly(
        DatetimeLikeArrayMixin._hasnans.fget  # type: ignore[attr-defined]
    )
    _hasnans = hasnans  # for index / array -agnostic code

    @property
    def is_all_dates(self) -> bool:
        return True

    # ------------------------------------------------------------------------
    # Abstract data attributes

    @property
    def values(self):
        # Note: PeriodArray overrides this to return an ndarray of objects.
        return self._data._data

    def __array_wrap__(self, result, context=None):
        """
        Gets called after a ufunc and other functions.
        """
        result = lib.item_from_zerodim(result)
        if is_bool_dtype(result) or lib.is_scalar(result):
            return result

        attrs = self._get_attributes_dict()
        if not is_period_dtype(self.dtype) and attrs["freq"]:
            # no need to infer if freq is None
            attrs["freq"] = "infer"
        return Index(result, **attrs)

    # ------------------------------------------------------------------------

    def equals(self, other: object) -> bool:
        """
        Determines if two Index objects contain the same elements.
        """
        if self.is_(other):
            return True

        if not isinstance(other, Index):
            return False
        elif not isinstance(other, type(self)):
            try:
                other = type(self)(other)
            except (ValueError, TypeError, OverflowError):
                # e.g.
                #  ValueError -> cannot parse str entry, or OutOfBoundsDatetime
                #  TypeError  -> trying to convert IntervalIndex to DatetimeIndex
                #  OverflowError -> Index([very_large_timedeltas])
                return False

        if not is_dtype_equal(self.dtype, other.dtype):
            # have different timezone
            return False

        return np.array_equal(self.asi8, other.asi8)

    @Appender(Index.__contains__.__doc__)
    def __contains__(self, key: Any) -> bool:
        hash(key)
        try:
            res = self.get_loc(key)
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            is_scalar(res) or isinstance(res, slice) or (is_list_like(res) and len(res))
        )

    def sort_values(self, return_indexer=False, ascending=True, key=None):
        """
        Return sorted copy of Index.
        """
        idx = ensure_key_mapped(self, key)

        _as = idx.argsort()
        if not ascending:
            _as = _as[::-1]
        sorted_index = self.take(_as)

        if return_indexer:
            return sorted_index, _as
        else:
            return sorted_index

    @Appender(_index_shared_docs["take"] % _index_doc_kwargs)
    def take(self, indices, axis=0, allow_fill=True, fill_value=None, **kwargs):
        nv.validate_take(tuple(), kwargs)
        indices = ensure_int64(indices)

        maybe_slice = lib.maybe_indices_to_slice(indices, len(self))
        if isinstance(maybe_slice, slice):
            return self[maybe_slice]

        return ExtensionIndex.take(
            self, indices, axis, allow_fill, fill_value, **kwargs
        )

    @doc(IndexOpsMixin.searchsorted, klass="Datetime-like Index")
    def searchsorted(self, value, side="left", sorter=None):
        if isinstance(value, str):
            raise TypeError(
                "searchsorted requires compatible dtype or scalar, "
                f"not {type(value).__name__}"
            )
        if isinstance(value, Index):
            value = value._data

        return self._data.searchsorted(value, side=side, sorter=sorter)

    _can_hold_na = True

    _na_value = NaT
    """The expected NA value to use with this index."""

    def _convert_tolerance(self, tolerance, target):
        tolerance = np.asarray(to_timedelta(tolerance).to_numpy())

        if target.size != tolerance.size and tolerance.size > 1:
            raise ValueError("list-like tolerance size must match target index size")
        return tolerance

    def tolist(self) -> List:
        """
        Return a list of the underlying data.
        """
        return list(self.astype(object))

    def min(self, axis=None, skipna=True, *args, **kwargs):
        """
        Return the minimum value of the Index or minimum along
        an axis.

        See Also
        --------
        numpy.ndarray.min
        Series.min : Return the minimum value in a Series.
        """
        nv.validate_min(args, kwargs)
        nv.validate_minmax_axis(axis)

        if not len(self):
            return self._na_value

        i8 = self.asi8
        try:
            # quick check
            if len(i8) and self.is_monotonic:
                if i8[0] != iNaT:
                    return self._box_func(i8[0])

            if self.hasnans:
                if skipna:
                    min_stamp = self[~self._isnan].asi8.min()
                else:
                    return self._na_value
            else:
                min_stamp = i8.min()
            return self._box_func(min_stamp)
        except ValueError:
            return self._na_value

    def argmin(self, axis=None, skipna=True, *args, **kwargs):
        """
        Returns the indices of the minimum values along an axis.

        See `numpy.ndarray.argmin` for more information on the
        `axis` parameter.

        See Also
        --------
        numpy.ndarray.argmin
        """
        nv.validate_argmin(args, kwargs)
        nv.validate_minmax_axis(axis)

        i8 = self.asi8
        if self.hasnans:
            mask = self._isnan
            if mask.all() or not skipna:
                return -1
            i8 = i8.copy()
            i8[mask] = np.iinfo("int64").max
        return i8.argmin()

    def max(self, axis=None, skipna=True, *args, **kwargs):
        """
        Return the maximum value of the Index or maximum along
        an axis.

        See Also
        --------
        numpy.ndarray.max
        Series.max : Return the maximum value in a Series.
        """
        nv.validate_max(args, kwargs)
        nv.validate_minmax_axis(axis)

        if not len(self):
            return self._na_value

        i8 = self.asi8
        try:
            # quick check
            if len(i8) and self.is_monotonic:
                if i8[-1] != iNaT:
                    return self._box_func(i8[-1])

            if self.hasnans:
                if skipna:
                    max_stamp = self[~self._isnan].asi8.max()
                else:
                    return self._na_value
            else:
                max_stamp = i8.max()
            return self._box_func(max_stamp)
        except ValueError:
            return self._na_value

    def argmax(self, axis=None, skipna=True, *args, **kwargs):
        """
        Returns the indices of the maximum values along an axis.

        See `numpy.ndarray.argmax` for more information on the
        `axis` parameter.

        See Also
        --------
        numpy.ndarray.argmax
        """
        nv.validate_argmax(args, kwargs)
        nv.validate_minmax_axis(axis)

        i8 = self.asi8
        if self.hasnans:
            mask = self._isnan
            if mask.all() or not skipna:
                return -1
            i8 = i8.copy()
            i8[mask] = 0
        return i8.argmax()

    # --------------------------------------------------------------------
    # Rendering Methods

    def format(
        self,
        name: bool = False,
        formatter: Optional[Callable] = None,
        na_rep: str = "NaT",
        date_format: Optional[str] = None,
    ) -> List[str]:
        """
        Render a string representation of the Index.
        """
        header = []
        if name:
            fmt_name = ibase.pprint_thing(self.name, escape_chars=("\t", "\r", "\n"))
            header.append(fmt_name)

        if formatter is not None:
            return header + list(self.map(formatter))

        return self._format_with_header(header, na_rep=na_rep, date_format=date_format)

    def _format_with_header(self, header, na_rep="NaT", date_format=None) -> List[str]:
        return header + list(
            self._format_native_types(na_rep=na_rep, date_format=date_format)
        )

    @property
    def _formatter_func(self):
        raise AbstractMethodError(self)

    def _format_attrs(self):
        """
        Return a list of tuples of the (attr,formatted_value).
        """
        attrs = super()._format_attrs()
        for attrib in self._attributes:
            if attrib == "freq":
                freq = self.freqstr
                if freq is not None:
                    freq = repr(freq)
                attrs.append(("freq", freq))
        return attrs

    # --------------------------------------------------------------------
    # Indexing Methods

    def _validate_partial_date_slice(self, reso: Resolution):
        raise NotImplementedError

    def _parsed_string_to_bounds(self, reso: Resolution, parsed: datetime):
        raise NotImplementedError

    def _partial_date_slice(
        self,
        reso: Resolution,
        parsed: datetime,
        use_lhs: bool = True,
        use_rhs: bool = True,
    ):
        """
        Parameters
        ----------
        reso : Resolution
        parsed : datetime
        use_lhs : bool, default True
        use_rhs : bool, default True

        Returns
        -------
        slice or ndarray[intp]
        """
        self._validate_partial_date_slice(reso)

        t1, t2 = self._parsed_string_to_bounds(reso, parsed)
        i8vals = self.asi8
        unbox = self._data._unbox_scalar

        if self.is_monotonic:

            if len(self) and (
                (use_lhs and t1 < self[0] and t2 < self[0])
                or ((use_rhs and t1 > self[-1] and t2 > self[-1]))
            ):
                # we are out of range
                raise KeyError

            # TODO: does this depend on being monotonic _increasing_?

            # a monotonic (sorted) series can be sliced
            # Use asi8.searchsorted to avoid re-validating Periods/Timestamps
            left = i8vals.searchsorted(unbox(t1), side="left") if use_lhs else None
            right = i8vals.searchsorted(unbox(t2), side="right") if use_rhs else None
            return slice(left, right)

        else:
            lhs_mask = (i8vals >= unbox(t1)) if use_lhs else True
            rhs_mask = (i8vals <= unbox(t2)) if use_rhs else True

            # try to find the dates
            return (lhs_mask & rhs_mask).nonzero()[0]

    # --------------------------------------------------------------------
    # Arithmetic Methods

    __add__ = make_wrapped_arith_op("__add__")
    __sub__ = make_wrapped_arith_op("__sub__")
    __radd__ = make_wrapped_arith_op("__radd__")
    __rsub__ = make_wrapped_arith_op("__rsub__")
    __pow__ = make_wrapped_arith_op("__pow__")
    __rpow__ = make_wrapped_arith_op("__rpow__")
    __mul__ = make_wrapped_arith_op("__mul__")
    __rmul__ = make_wrapped_arith_op("__rmul__")
    __floordiv__ = make_wrapped_arith_op("__floordiv__")
    __rfloordiv__ = make_wrapped_arith_op("__rfloordiv__")
    __mod__ = make_wrapped_arith_op("__mod__")
    __rmod__ = make_wrapped_arith_op("__rmod__")
    __divmod__ = make_wrapped_arith_op("__divmod__")
    __rdivmod__ = make_wrapped_arith_op("__rdivmod__")
    __truediv__ = make_wrapped_arith_op("__truediv__")
    __rtruediv__ = make_wrapped_arith_op("__rtruediv__")

    def isin(self, values, level=None):
        """
        Compute boolean array of whether each index value is found in the
        passed set of values.

        Parameters
        ----------
        values : set or sequence of values

        Returns
        -------
        is_contained : ndarray (boolean dtype)
        """
        if level is not None:
            self._validate_index_level(level)

        if not isinstance(values, type(self)):
            try:
                values = type(self)(values)
            except ValueError:
                return self.astype(object).isin(values)

        return algorithms.isin(self.asi8, values.asi8)

    @Appender(Index.where.__doc__)
    def where(self, cond, other=None):
        values = self.view("i8")

        try:
            other = self._data._validate_where_value(other)
        except (TypeError, ValueError) as err:
            # Includes tzawareness mismatch and IncompatibleFrequencyError
            oth = getattr(other, "dtype", other)
            raise TypeError(f"Where requires matching dtype, not {oth}") from err

        result = np.where(cond, values, other).astype("i8")
        arr = type(self._data)._simple_new(result, dtype=self.dtype)
        return type(self)._simple_new(arr, name=self.name)

    def _summary(self, name=None) -> str:
        """
        Return a summarized representation.

        Parameters
        ----------
        name : str
            Name to use in the summary representation.

        Returns
        -------
        str
            Summarized representation of the index.
        """
        formatter = self._formatter_func
        if len(self) > 0:
            index_summary = f", {formatter(self[0])} to {formatter(self[-1])}"
        else:
            index_summary = ""

        if name is None:
            name = type(self).__name__
        result = f"{name}: {len(self)} entries{index_summary}"
        if self.freq:
            result += f"\nFreq: {self.freqstr}"

        # display as values, not quoted
        result = result.replace("'", "")
        return result

    def shift(self, periods=1, freq=None):
        """
        Shift index by desired number of time frequency increments.

        This method is for shifting the values of datetime-like indexes
        by a specified time increment a given number of times.

        Parameters
        ----------
        periods : int, default 1
            Number of periods (or increments) to shift by,
            can be positive or negative.

            .. versionchanged:: 0.24.0

        freq : pandas.DateOffset, pandas.Timedelta or string, optional
            Frequency increment to shift by.
            If None, the index is shifted by its own `freq` attribute.
            Offset aliases are valid strings, e.g., 'D', 'W', 'M' etc.

        Returns
        -------
        pandas.DatetimeIndex
            Shifted index.

        See Also
        --------
        Index.shift : Shift values of Index.
        PeriodIndex.shift : Shift values of PeriodIndex.
        """
        arr = self._data.view()
        arr._freq = self.freq
        result = arr._time_shift(periods, freq=freq)
        return type(self)(result, name=self.name)

    # --------------------------------------------------------------------
    # List-like Methods

    def delete(self, loc):
        new_i8s = np.delete(self.asi8, loc)

        freq = None
        if is_period_dtype(self.dtype):
            freq = self.freq
        elif is_integer(loc):
            if loc in (0, -len(self), -1, len(self) - 1):
                freq = self.freq
        else:
            if is_list_like(loc):
                loc = lib.maybe_indices_to_slice(ensure_int64(np.array(loc)), len(self))
            if isinstance(loc, slice) and loc.step in (1, None):
                if loc.start in (0, None) or loc.stop in (len(self), None):
                    freq = self.freq

        arr = type(self._data)._simple_new(new_i8s, dtype=self.dtype, freq=freq)
        return type(self)._simple_new(arr, name=self.name)

    # --------------------------------------------------------------------
    # Join/Set Methods

    def _wrap_joined_index(self, joined: np.ndarray, other):
        assert other.dtype == self.dtype, (other.dtype, self.dtype)
        name = get_op_result_name(self, other)

        if is_period_dtype(self.dtype):
            freq = self.freq
        else:
            self = cast(DatetimeTimedeltaMixin, self)
            freq = self.freq if self._can_fast_union(other) else None
        new_data = type(self._data)._simple_new(joined, dtype=self.dtype, freq=freq)

        return type(self)._simple_new(new_data, name=name)

    @doc(Index._convert_arr_indexer)
    def _convert_arr_indexer(self, keyarr):
        if lib.infer_dtype(keyarr) == "string":
            # Weak reasoning that indexer is a list of strings
            # representing datetime or timedelta or period
            try:
                extension_arr = pd_array(keyarr, self.dtype)
            except (ValueError, DateParseError):
                # Fail to infer keyarr from self.dtype
                return keyarr

            converted_arr = extract_array(extension_arr, extract_numpy=True)
        else:
            converted_arr = com.asarray_tuplesafe(keyarr)
        return converted_arr


class DatetimeTimedeltaMixin(DatetimeIndexOpsMixin, Int64Index):
    """
    Mixin class for methods shared by DatetimeIndex and TimedeltaIndex,
    but not PeriodIndex
    """

    # Compat for frequency inference, see GH#23789
    _is_monotonic_increasing = Index.is_monotonic_increasing
    _is_monotonic_decreasing = Index.is_monotonic_decreasing
    _is_unique = Index.is_unique

    def _with_freq(self, freq):
        arr = self._data._with_freq(freq)
        return type(self)._simple_new(arr, name=self.name)

    def _shallow_copy(self, values=None, name: Label = lib.no_default):
        name = self.name if name is lib.no_default else name
        cache = self._cache.copy() if values is None else {}

        if values is None:
            values = self._data

        if isinstance(values, np.ndarray):
            # TODO: We would rather not get here
            values = type(self._data)(values, dtype=self.dtype)

        result = type(self)._simple_new(values, name=name)
        result._cache = cache
        return result

    # --------------------------------------------------------------------
    # Set Operation Methods

    @Appender(Index.difference.__doc__)
    def difference(self, other, sort=None):
        new_idx = super().difference(other, sort=sort)._with_freq(None)
        return new_idx

    def intersection(self, other, sort=False):
        """
        Specialized intersection for DatetimeIndex/TimedeltaIndex.

        May be much faster than Index.intersection

        Parameters
        ----------
        other : Same type as self or array-like
        sort : False or None, default False
            Sort the resulting index if possible.

            .. versionadded:: 0.24.0

            .. versionchanged:: 0.24.1

               Changed the default to ``False`` to match the behaviour
               from before 0.24.0.

            .. versionchanged:: 0.25.0

               The `sort` keyword is added

        Returns
        -------
        y : Index or same type as self
        """
        self._validate_sort_keyword(sort)
        self._assert_can_do_setop(other)
        res_name = get_op_result_name(self, other)

        if self.equals(other):
            return self._get_reconciled_name_object(other)

        if len(self) == 0:
            return self.copy()
        if len(other) == 0:
            return other.copy()

        if not isinstance(other, type(self)):
            result = Index.intersection(self, other, sort=sort)
            if isinstance(result, type(self)):
                if result.freq is None:
                    # TODO: no tests rely on this; needed?
                    result = result._with_freq("infer")
            result.name = res_name
            return result

        elif not self._can_fast_intersect(other):
            result = Index.intersection(self, other, sort=sort)
            # We need to invalidate the freq because Index.intersection
            #  uses _shallow_copy on a view of self._data, which will preserve
            #  self.freq if we're not careful.
            result = result._with_freq(None)._with_freq("infer")
            result.name = res_name
            return result

        # to make our life easier, "sort" the two ranges
        if self[0] <= other[0]:
            left, right = self, other
        else:
            left, right = other, self

        # after sorting, the intersection always starts with the right index
        # and ends with the index of which the last elements is smallest
        end = min(left[-1], right[-1])
        start = right[0]

        if end < start:
            return type(self)(data=[], dtype=self.dtype, freq=self.freq, name=res_name)
        else:
            lslice = slice(*left.slice_locs(start, end))
            left_chunk = left._values[lslice]
            return type(self)._simple_new(left_chunk, name=res_name)

    def _can_fast_intersect(self: _T, other: _T) -> bool:
        if self.freq is None:
            return False

        elif other.freq != self.freq:
            return False

        elif not self.is_monotonic_increasing:
            # Because freq is not None, we must then be monotonic decreasing
            return False

        elif self.freq.is_anchored():
            # this along with matching freqs ensure that we "line up",
            #  so intersection will preserve freq
            return True

        elif isinstance(self.freq, Tick):
            # We "line up" if and only if the difference between two of our points
            #  is a multiple of our freq
            diff = self[0] - other[0]
            remainder = diff % self.freq.delta
            return remainder == Timedelta(0)

        return True

    def _can_fast_union(self: _T, other: _T) -> bool:
        # Assumes that type(self) == type(other), as per the annotation
        # The ability to fast_union also implies that `freq` should be
        #  retained on union.
        if not isinstance(other, type(self)):
            return False

        freq = self.freq

        if freq is None or freq != other.freq:
            return False

        if not self.is_monotonic_increasing:
            # Because freq is not None, we must then be monotonic decreasing
            # TODO: do union on the reversed indexes?
            return False

        if len(self) == 0 or len(other) == 0:
            return True

        # to make our life easier, "sort" the two ranges
        if self[0] <= other[0]:
            left, right = self, other
        else:
            left, right = other, self

        right_start = right[0]
        left_end = left[-1]

        # Only need to "adjoin", not overlap
        return (right_start == left_end + freq) or right_start in left

    def _fast_union(self, other, sort=None):
        if len(other) == 0:
            return self.view(type(self))

        if len(self) == 0:
            return other.view(type(self))

        # to make our life easier, "sort" the two ranges
        if self[0] <= other[0]:
            left, right = self, other
        elif sort is False:
            # TDIs are not in the "correct" order and we don't want
            #  to sort but want to remove overlaps
            left, right = self, other
            left_start = left[0]
            loc = right.searchsorted(left_start, side="left")
            right_chunk = right._values[:loc]
            dates = concat_compat((left._values, right_chunk))
            # With sort being False, we can't infer that result.freq == self.freq
            # TODO: no tests rely on the _with_freq("infer"); needed?
            result = self._shallow_copy(dates)._with_freq("infer")
            return result
        else:
            left, right = other, self

        left_end = left[-1]
        right_end = right[-1]

        # concatenate
        if left_end < right_end:
            loc = right.searchsorted(left_end, side="right")
            right_chunk = right._values[loc:]
            dates = concat_compat([left._values, right_chunk])
            # The can_fast_union check ensures that the result.freq
            #  should match self.freq
            dates = type(self._data)(dates, freq=self.freq)
            result = type(self)._simple_new(dates, name=self.name)
            return result
        else:
            return left

    def _union(self, other, sort):
        if not len(other) or self.equals(other) or not len(self):
            return super()._union(other, sort=sort)

        # We are called by `union`, which is responsible for this validation
        assert isinstance(other, type(self))

        this, other = self._maybe_utc_convert(other)

        if this._can_fast_union(other):
            result = this._fast_union(other, sort=sort)
            if sort is None:
                # In the case where sort is None, _can_fast_union
                #  implies that result.freq should match self.freq
                assert result.freq == self.freq, (result.freq, self.freq)
            elif result.freq is None:
                # TODO: no tests rely on this; needed?
                result = result._with_freq("infer")
            return result
        else:
            i8self = Int64Index._simple_new(self.asi8, name=self.name)
            i8other = Int64Index._simple_new(other.asi8, name=other.name)
            i8result = i8self._union(i8other, sort=sort)
            result = type(self)(i8result, dtype=self.dtype, freq="infer")
            return result

    # --------------------------------------------------------------------
    # Join Methods
    _join_precedence = 10

    _inner_indexer = _join_i8_wrapper(libjoin.inner_join_indexer)
    _outer_indexer = _join_i8_wrapper(libjoin.outer_join_indexer)
    _left_indexer = _join_i8_wrapper(libjoin.left_join_indexer)
    _left_indexer_unique = _join_i8_wrapper(
        libjoin.left_join_indexer_unique, with_indexers=False
    )

    def join(
        self, other, how: str = "left", level=None, return_indexers=False, sort=False
    ):
        """
        See Index.join
        """
        if self._is_convertible_to_index_for_join(other):
            try:
                other = type(self)(other)
            except (TypeError, ValueError):
                pass

        this, other = self._maybe_utc_convert(other)
        return Index.join(
            this,
            other,
            how=how,
            level=level,
            return_indexers=return_indexers,
            sort=sort,
        )

    def _maybe_utc_convert(self, other):
        this = self
        if not hasattr(self, "tz"):
            return this, other

        if isinstance(other, type(self)):
            if self.tz is not None:
                if other.tz is None:
                    raise TypeError("Cannot join tz-naive with tz-aware DatetimeIndex")
            elif other.tz is not None:
                raise TypeError("Cannot join tz-naive with tz-aware DatetimeIndex")

            if not timezones.tz_compare(self.tz, other.tz):
                this = self.tz_convert("UTC")
                other = other.tz_convert("UTC")
        return this, other

    @classmethod
    def _is_convertible_to_index_for_join(cls, other: Index) -> bool:
        """
        return a boolean whether I can attempt conversion to a
        DatetimeIndex/TimedeltaIndex
        """
        if isinstance(other, cls):
            return False
        elif len(other) > 0 and other.inferred_type not in (
            "floating",
            "mixed-integer",
            "integer",
            "integer-na",
            "mixed-integer-float",
            "mixed",
        ):
            return True
        return False

    # --------------------------------------------------------------------
    # List-Like Methods

    def insert(self, loc, item):
        """
        Make new Index inserting new item at location

        Parameters
        ----------
        loc : int
        item : object
            if not either a Python datetime or a numpy integer-like, returned
            Index dtype will be object rather than datetime.

        Returns
        -------
        new_index : Index
        """
        if isinstance(item, str):
            # TODO: Why are strings special?
            # TODO: Should we attempt _scalar_from_string?
            return self.astype(object).insert(loc, item)

        item = self._data._validate_insert_value(item)

        freq = None
        # check freq can be preserved on edge cases
        if self.freq is not None:
            if self.size:
                if item is NaT:
                    pass
                elif (loc == 0 or loc == -len(self)) and item + self.freq == self[0]:
                    freq = self.freq
                elif (loc == len(self)) and item - self.freq == self[-1]:
                    freq = self.freq
            else:
                # Adding a single item to an empty index may preserve freq
                if self.freq.is_on_offset(item):
                    freq = self.freq

        item = self._data._unbox_scalar(item)

        new_i8s = np.concatenate([self[:loc].asi8, [item], self[loc:].asi8])
        arr = type(self._data)._simple_new(new_i8s, dtype=self.dtype, freq=freq)
        return type(self)._simple_new(arr, name=self.name)
