#!/usr/bin/env python3
"""
Includes miscellaneous mathematical functions.
"""
import datetime
from functools import partial

import numpy as np
import pandas as pd
import xarray as xr

from .diff import deriv_half, deriv_uneven
from .internals import ic  # noqa: F401
from .internals import quack, warnings

__all__ = [
    'calendar',
    'intersection',
    'linetrack',
    'match',
    'zerofind',
]


def calendar(dt, /):
    """
    Convert an array of datetime64 values to a calendar array of years, months, days,
    hours, minutes, and seconds. Adds a trailing axis of length 6.

    Parameters
    ----------
    dt : datetime array
        A datetime array with arbitrary shape. May be a `pandas.DatetimeIndex`
        array, a `numpy.datetime64` array, or an object-type array of native
        python `datetime.datetime` instances.

    Returns
    -------
    cal : uint32 array (..., 6)
        A calendar array matching the shape of the input array up to the rightmost axis.
        The rightmost axis is length 6; its indices contain the years, months, days,
        hours, minutes, and seconds of the input datetimes.

    Examples
    --------
    >>> import pandas as pd
    >>> import climopy as climo
    >>> idx = pd.date_range('2000-01-01', '2000-01-03', freq='450T')
    >>> climo.calendar(idx)
    array([[2000,    1,    1,    0,    0,    0],
           [2000,    1,    1,    7,   30,    0],
           [2000,    1,    1,   15,    0,    0],
           [2000,    1,    1,   22,   30,    0],
           [2000,    1,    2,    6,    0,    0],
           [2000,    1,    2,   13,   30,    0],
           [2000,    1,    2,   21,    0,    0]], dtype=uint32)
    """
    # See: https://stackoverflow.com/a/56260054/4970632
    # Allocate output
    # NOTE: asanyarray passes MaskedArrays through while asarray does not
    fancy = isinstance(dt, (pd.Index, pd.Series, xr.DataArray))
    if not fancy:
        dt = np.asanyarray(dt)
    out = np.empty(dt.shape + (6,), dtype='u4')

    # Decompose calendar floors
    # NOTE: M8 is datetime64, m8 is timedelta64
    if fancy:
        # Datatype is subdtype of numpy.datetime64 but array container includes builtin
        # methods for getting calendar properties
        if isinstance(dt, xr.DataArray):
            dt = dt.dt  # use builtin xarray datetime accessor
        out[..., 0] = dt.year
        out[..., 1] = dt.month
        out[..., 2] = dt.day
        out[..., 3] = dt.hour
        out[..., 4] = dt.minute
        out[..., 5] = dt.second
    elif np.issubdtype(dt, np.datetime64):
        Y, M, D, h, m, s = [dt.astype(f'M8[{x}]') for x in 'YMDhms']
        out[..., 0] = Y + 1970  # Gregorian Year
        out[..., 1] = (M - Y) + 1  # month
        out[..., 2] = (D - M) + 1  # day
        out[..., 3] = (dt - D).astype('m8[h]')  # hour
        out[..., 4] = (dt - h).astype('m8[m]')  # minute
        out[..., 5] = (dt - m).astype('m8[s]')  # second
    elif dt.dtype == 'object' and all(isinstance(_, datetime.datetime) for _ in dt.flat):  # noqa: E501
        out[..., 0] = np.vectorize(partial(getattr, dt, 'year'))()
        out[..., 1] = np.vectorize(partial(getattr, dt, 'month'))()
        out[..., 2] = np.vectorize(partial(getattr, dt, 'day'))()
        out[..., 3] = np.vectorize(partial(getattr, dt, 'hour'))()
        out[..., 4] = np.vectorize(partial(getattr, dt, 'minute'))()
        out[..., 5] = np.vectorize(partial(getattr, dt, 'second'))()
    else:
        raise ValueError(f'Invalid data type for calendar(): {dt.dtype}')
    return out


def match(*args):
    """
    Return the overlapping segment from a series of vectors with common coordinate
    spacings but different start or end points. Useful e.g. for matching the time
    dimensions of variables collected over different but overlapping date ranges.

    Parameters
    ----------
    v1, v2, ... : ndarray
        The coordinate vectors.

    Returns
    -------
    i1, i2, ..., v : ndarray
        The `slice` objects that index matching coordinate ranges for each vector, and
        a vector containing these coordinates.

    Examples
    --------
    >>> import pandas as pd
    >>> import climopy as climo
    >>> v1 = pd.date_range('2000-01-01', '2000-01-10')
    >>> v2 = pd.date_range('2000-01-05', '2000-01-15')
    >>> i1, i2, v = climo.match(v1, v2)
    >>> v
    DatetimeIndex(['2000-01-05', '2000-01-06', '2000-01-07', '2000-01-08',
                   '2000-01-09', '2000-01-10'],
                  dtype='datetime64[ns]', freq='D')
    >>> np.all(v1[i1] == v2[i2])
    True
    >>> np.all(v1[i1] == v)
    True
    """
    vs = quack._as_arraylike(*args)
    if not all(np.all(v == np.sort(v)) for v in vs):
        raise ValueError('Vectors must be sorted.')

    # Get common minima/maxima
    min_ = max(map(np.min, vs))
    max_ = min(map(np.max, vs))
    try:
        min_idx = [np.where(v == min_)[0][0] for v in vs]
        max_idx = [np.where(v == max_)[0][0] for v in vs]
    except IndexError:
        raise ValueError('Vectors do not have matching minima/maxima.')
    slices = [slice(i, j + 1) for i, j in zip(min_idx, max_idx)]

    # Checks
    if any(
        v[slice_].size != vs[0][slices[0]].size
        for v, slice_ in zip(vs, slices)
    ):
        raise ValueError('Vectors are not identical between matching minima/maxima.')
    elif any(
        not np.all(v[slice_] == vs[0][slices[0]])
        for v, slice_ in zip(vs, slices)
    ):
        raise ValueError('Vectors are not identical between matching minima/maxima.')
    return slices + [vs[0][slices[0]]]


def intersection(x, y1, y2, /, xlog=False):
    """
    Find the (first) intersection point for two line segments.

    Parameters
    ----------
    x : ndarray
        The *x* coordinates.
    y1, y2 : ndarray
        The two lists of *y* coordinates.
    xlog : bool, optional
        Whether to find the *x* coordinate intersection in logarithmic space.

    Examples
    --------
    >>> import climopy as climo
    >>> x = 10 + np.arange(4)
    >>> y1 = np.array([4, 2, 0, -2])
    >>> y2 = np.array([0, 1, 2, 3])
    >>> climo.intersection(x, y1, y2)
    (11.333333333333334, 1.333333333333334)
    """
    # Initial stuff
    x = np.asanyarray(x)
    y1 = np.asanyarray(y1)
    y2 = np.asanyarray(y2)
    if xlog:  # transform x coordinates optionally
        transform = lambda x: np.log10(x)  # noqa: E731
        itransform = lambda x: 10 ** x  # noqa: E731
    else:
        transform = itransform = lambda x: x  # noqa: E731
    if x.size != y1.size or x.size != y2.size:
        raise ValueError(f'Incompatible sizes {x.size=}, {y1.size=}, {y2.size=}.')

    # Get intersection
    dy = y1 - y2
    if np.all(dy > 0) or np.all(dy < 0):
        warnings._warn_climopy(f'No intersections found for data {y1!r} and {y2!r}.')
        return np.nan, np.nan
    idx, = np.where(np.diff(np.sign(dy)) != 0)  # e.g. 6, 2, -3 --> 1, 1, -1 --> 0, -2
    if idx.size > 1:
        warnings._warn_climopy('Multiple intersections found. Using the first one.')
    idx = idx[0]

    # Get coordinates
    x, y = dy[idx:idx + 2], transform(x[idx:idx + 2])
    px = itransform(y[0] + (0 - x[0]) * ((y[1] - y[0]) / (x[1] - x[0])))
    x, y = y, y2[idx:idx + 2]
    py = y[0] + (transform(px) - x[0]) * ((y[1] - y[0]) / (x[1] - x[0]))
    return px, py


# TODO: Support pint quantities here
def linetrack(xs, ys=None, /, ntrack=None, seed=None, sep=None):  # noqa: E225
    """
    Track individual "lines" across lists of coordinates.

    Parameters
    ----------
    xs : list of lists
        The locations to be grouped into tracks.
    ys : list of lists, optional
        The values corresponding to the locations `xs`.
    ntrack : int, optional
        The maximum number of values to be simultaneously tracked. This can be used
        in combination with `seed` to ignore spurious tracks. The default value is
        `numpy.inf` (i.e. the number of tracks is unlimited).
    seed : float or list of float, optional
        Seed value(s) for the track(s) that should be picked up at the start.
        If `ntrack` is ``None`` this has no effect.
    sep : float, optional
        The maximum separation between points belonging to the same "track". If a
        separation is larger than `sep` the algorithm will begin a new track. Default
        is `numpy.inf` (i.e. tracks are never ended due to "large" separations).

    Returns
    -------
    xs_sorted : ndarray
        2D array of *x* coordinates whose columns correspond to individual "tracks".
        Tracks may stop or start at rows in the middle of the array.
    ys_sorted : ndarray, optional
        The corresponding *y* coordinates. Returned if `ys` is not ``None``.

    Examples
    --------
    >>> import climopy as climo
    >>> climo.linetrack(
    ...    [
    ...        [30, 20],
    ...        [22],
    ...        [24],
    ...        [32, 25],
    ...        [26, 40, 33],
    ...        [45],
    ...        [20, 47],
    ...        [23, 50],
    ...    ]
    ... )
    array([[30., 20., nan],
           [nan, 22., nan],
           [nan, 24., nan],
           [32., 25., nan],
           [33., 26., 40.],
           [nan, nan, 45.],
           [20., nan, 47.],
           [23., nan, 50.]])
    """
    # Parse input
    if ys is None:
        ys = xs  # not pretty but this simplifies the loop code
    if sep is None:
        sep = np.inf
    if seed is None:
        seed = []
    if (
        len(xs) != len(ys)
        or any(np.atleast_1d(x).size != np.atleast_1d(y).size for x, y in zip(xs, ys))
    ):
        raise ValueError('Mismatched geometry between x and y lines.')
    if ntrack is None:
        # WARNING: np.isscalar(np.array(1)) returns False so need to take
        # great pains to avoid length of unsized object errors
        ntrack = max(
            size if (size := getattr(x, 'size', None)) is not None  # noqa: E203, E231
            else 1 if np.isscalar(x) else len(x) for x in xs
        )

    # Arrays containing sorted lines in the output columns
    # NOTE: Need twice the maximum number of simultaneously tracked lines as columns
    # in the array. For example the following sequence with ntrack == 1 and sep == 5:
    # [20, NaN]
    # [22, NaN]
    # [NaN, 40]  # bigger than sep, so "new" line
    # For another example, the following sequence with ntrack == 2 and sep == np.inf:
    # [18, 32, NaN]
    # [20, 30, NaN]
    # [NaN, 33, 40]
    # The algorithm recognizes that even if ntrack is 2, if the remaining unmatched
    # points are even *farther* from the remaining previous points, this is a new line.
    nslots = 2 * ntrack
    seed = np.atleast_1d(seed)[:ntrack]
    with np.errstate(invalid='ignore'):
        xs_sorted = np.empty((len(xs) + 1, nslots)) * np.nan
        ys_sorted = np.empty((len(ys) + 1, nslots)) * np.nan
    xs_sorted[0, :seed.size] = seed

    for i, (ixs, iys) in enumerate(zip(xs, ys)):
        i += 1
        # Simple cases: No line tracking, no lines in this group, *or* no
        # lines in previous group so every single point starts a new line.
        # NOTE: It's ok if columns are occupied by more than one "line" as
        # long as there are NaNs between them. This is really just for plotting.
        ixs = np.atleast_1d(ixs)
        iys = np.atleast_1d(iys)
        if ixs.size == 0 or np.all(np.isnan(xs_sorted[i - 1, :])):
            ixs = ixs[:ntrack]  # WARNING: just truncate the list of candidates!
            iys = iys[:ntrack]
            xs_sorted[i, :ixs.size] = ixs
            ys_sorted[i, :iys.size] = iys
            continue

        # Find the points in the latest unsorted record that are *closest*
        # to the points in existing tracks, and the difference between them.
        with np.errstate(invalid='ignore'):
            mindiffs = np.empty((nslots,)) * np.nan
            argmins = np.empty((nslots,)) * np.nan
        for j, ix_prev in enumerate(xs_sorted[i - 1, :]):
            if np.isnan(ix_prev):
                continue
            diffs = np.abs(ixs - ix_prev)
            if np.min(diffs) > sep:
                continue  # not a member of *any* existing track
            mindiffs[j] = np.min(diffs)
            argmins[j] = np.argmin(diffs)

        # Handle *existing* tracks that continued or died out
        # Note that NaNs always go last in an argsort
        idxs = set()
        nlines = 0
        lines_old = np.argsort(mindiffs)  # prefer *smallest* differences
        for j in lines_old:
            idx = argmins[j]
            if np.isnan(idx):  # track dies
                continue
            if idx in idxs:  # already continued the line from a closer candidate
                continue
            if nlines >= ntrack:
                continue
            nlines += 1
            idxs.add(idx)
            xs_sorted[i, j] = ixs[int(idx)]
            ys_sorted[i, j] = iys[int(idx)]

        # Handle brand new tracks
        # NOTE: Set comparison {1, 2, 3} - {1, 2, np.nan} is {3} (extra values omitted)
        # NOTE: Set comparison {1} - {1.0} is {} (no issues with mixed float/int types)
        # NOTE: Should never run out of jslots since 'nlines' limits possible lines
        # TODO: Better way to prioritize "new" lines than random approach
        jslots, = np.where(np.all(np.isnan(xs_sorted[i - 1:i + 1, :]), axis=0))
        lines_new = set(range(len(ixs))) - set(argmins)
        for j, idx in enumerate(lines_new):
            if nlines >= ntrack:
                continue
            nlines += 1
            xs_sorted[i, jslots[j]] = ixs[int(idx)]
            ys_sorted[i, jslots[j]] = iys[int(idx)]

    # Return lines ignoring the "seed" and removing empty tracks
    mask = np.any(~np.isnan(xs_sorted[1:, :]), axis=0)
    xs_sorted = xs_sorted[1:, mask]
    ys_sorted = ys_sorted[1:, mask]
    if xs is not ys:
        return xs_sorted, ys_sorted
    else:
        return xs_sorted


@quack._xarray_zerofind_wrapper
@quack._pint_wrapper(('=x', '=y'), ('=x', '=y'))
def zerofind(
    x, y, /, axis=-1, axis_track=-2, track=True, diff=None, centered=True, which='both',
    **kwargs,
):
    """
    Find the location of the zero value for a given data array.

    Parameters
    ----------
    x : array-like
        The coordinates.
    y : array-like
        The data for which we find zeros.
    axis : int, optional
        Axis along which zeros are found and (optionally) derivatives are taken.
    axis_track : int, optional
        Axis along which zeros taken along `axis` are "tracked".
    track : bool, optional
        Whether to track zeros. If ``False`` they are added in the order they appeared.
    diff : int, optional
        How many times to differentiate along the axis.
    centered : bool, optional
        Whether to use centered finite differencing or half level differencing.
    which : {'negpos', 'posneg', 'both'}, optional
        Whether to find values that go from negative to positive, positive
        to negative, or both (the ``'min'`` and ``'max'`` keys really
        only apply to when `diff` is ``1``).
    **kwargs
        Passed to `linetrack` and used to group the locations into
        coherent tracks.

    Returns
    -------
    zx : array-like
        The zero locations.
    zy : array-like
        The zero values. If ``diff == 0`` these should all be equal to zero
        up to floating point precision. Otherwise these are the minima and
        maxima corresponding to the zero derivative locations.

    Examples
    --------
    >>> import numpy as np
    >>> import xarray as xr
    >>> import climopy as climo
    >>> state = np.random.RandomState(51423)
    >>> ureg = climo.ureg
    >>> x = np.arange(100)
    >>> y = np.sort(state.rand(50, 10) - 0.5, axis=0)
    >>> y = np.concatenate((y, y[::-1, :]), axis=0)
    >>> xarr = xr.DataArray(
    ...     x * ureg.s,
    ...     dims=('x',), attrs={'long_name': 'x coordinate'}
    ... )
    >>> yarr = xr.DataArray(
    ...     y * ureg.m, name='variable',
    ...     dims=('x', 'y'), coords={'x': xarr.climo.dequantify()}
    ... )
    >>> zx, zy = climo.zerofind(xarr, yarr, axis=0, ntrack=2)
    >>> zx
    <xarray.DataArray 'x' (track: 2, y: 10)>
    <Quantity([[25.56712412 17.1468964  25.94590963 24.43748793 25.96456694 23.50805224
      24.26007638 25.76728476 26.30359681 22.41433647]
     [73.43287588 81.8531036  73.05409037 74.56251207 73.03543306 75.49194776
      74.73992362 73.23271524 72.69640319 76.58566353]], 'second')>
    Dimensions without coordinates: track, y
    Attributes:
        long_name:  x coordinate
    """
    # Tests
    # TODO: Support tracking across single axis
    if which not in ('negpos', 'posneg', 'both'):
        raise ValueError(f'Invalid which {which!r}.')
    if x.ndim != 1 or y.shape[axis] != x.size:
        raise ValueError(f'Invalid shapes {x.shape=} and {y.shape=}.')
    if x[1] - x[0] < 0:  # TODO: check this works?
        which = 'negpos' if which == 'posneg' else 'posneg' if which == 'negpos' else which  # noqa: E501
    ndim = y.ndim
    if ndim <= 2:
        axis, axis_track = -1, -2
        y = y[None, ...]
        if ndim == 1:
            y = y[None, ...]

    with quack._ArrayContext(y, push_right=(axis, axis_track)) as context:
        # Get flattened data and iterate over extra dimensions
        # NOTE: Axes are pushed to right in the specified order. Result: first axis
        # contains flattened extra dimensions, second axis is dimension along which
        # we are tracking zeros, and third axis is dimension across which we find zeros.
        ys = context.data
        zxs = []
        zys = []
        nextra, nalong, nacross = ys.shape
        for i in range(nextra):
            # Optionally take derivatives onto half-levels and interpolate to points
            # on those half-levels.
            # NOTE: Doesn't matter if units are degrees or meters for latitude.
            y = ys[i, :, :]
            dy = y
            if diff:  # not zero or None
                if centered:
                    # Centered differencing onto same levels
                    dy = deriv_uneven(x, y, axis=-1, order=diff, keepedges=True)
                else:
                    # More accurate differencing onto half levels
                    dx, dy = deriv_half(x, y, axis=-1, order=diff)
                    yi = dy.copy()
                    for i in range(nalong):
                        yi[i, :] = np.interp(dx, x, y[i, :])
                    x, y = dx, yi

            # Find where sign switches from +ve to -ve and vice versa
            zxs_along = []
            zys_along = []
            for k in range(nalong):
                # Get indices where vals go positive [to zero] to negative or vice versa
                # NOTE: Always have False where NaNs present
                posneg = negpos = ()
                with np.errstate(invalid='ignore'):
                    ddy = np.diff(np.sign(dy[k, :]))
                mask = np.zeros((ddy.size - 1,), dtype=bool)
                if which in ('negpos', 'both'):
                    mask = mask | (ddy[:-1] == 1) & (ddy[1:] == 1)  # *exact* zeros
                    negpos = ddy == 2
                if which in ('posneg', 'both'):
                    mask = mask | (ddy[:-1] == -1) & (ddy[1:] == -1)
                    posneg = ddy == -2

                # Record exact zero locations and values
                idxs, = np.where(mask)
                idxs += 1
                zxs_across = []
                zys_across = []
                for idx in idxs:
                    zxs_across.append(x[idx])
                    zys_across.append(y[k, idx])

                # Interpolate to inexact zero locations and values at those locations
                for j, mask in enumerate((negpos, posneg)):
                    idxs, = np.where(mask)  # NOTE: for empty array, yields nothing
                    for idx in idxs:
                        # Need dy to be *increasing* for numpy.interp to work
                        if j == 0:
                            slice_ = slice(idx, idx + 2)
                        else:
                            slice_ = slice(idx + 1, idx - 1, -1)
                        ix = x[slice_]
                        iy = y[k, slice_]
                        idy = dy[k, slice_]
                        if ix.size in (0, 1):
                            continue  # weird error
                        zx = np.interp(0, idy, ix, left=np.nan, right=np.nan)
                        if np.isnan(zx):  # no extrapolation!
                            continue
                        slice_ = slice(None) if ix[1] > ix[0] else slice(None, None, -1)
                        zy = np.interp(zx, ix[slice_], iy[slice_])
                        zxs_across.append(zx)
                        zys_across.append(zy)  # record

                # Add to list
                # NOTE: Must use lists because number of zeros varies
                zxs_along.append(zxs_across)
                zys_along.append(zys_across)

            # Optionally track values along particular axis
            if track:
                zxs_along, zys_along = linetrack(zxs_along, zys_along, **kwargs)
            else:
                ntrack = max(map(len, zxs_along))
                pad = lambda x: x + [np.nan] * (ntrack - len(x))  # noqa: E731
                zxs_along = np.vstack(list(map(pad, zxs_along)))
                zys_along = np.vstack(list(map(pad, zys_along)))
            zxs.append(zxs_along)
            zys.append(zys_along)

        # Concatenate arrays
        # NOTE: Last dimension is the track dimension so pad them first
        ntrack = max(_.shape[1] for _ in zxs)
        pad = lambda x: np.pad(x, ((0, 0), (0, ntrack - x.shape[1])), constant_values=np.nan)  # noqa: E501, E731
        zxs = np.vstack([_[None, ...] for _ in map(pad, zxs)])
        zys = np.vstack([_[None, ...] for _ in map(pad, zys)])

        # Add back as new data
        context.replace_data(zxs, zys)

    # Return unfurled data
    zxs, zys = context.data
    if ndim == 2:
        zxs, zys = zxs[0, ...], zys[0, ...]
    if ndim == 1:
        zxs, zys = zxs[0, 0, ...], zys[0, 0, ...]
    return zxs, zys
