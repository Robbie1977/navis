#    This script is part of navis (http://www.github.com/schlegelp/navis).
#    Copyright (C) 2018 Philipp Schlegel
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.

import nrrd
import os

import numpy as np

from glob import glob

import multiprocessing as mp

from typing import Union, Iterable, Optional, Dict, Any
from typing_extensions import Literal

from .. import config, utils, core

# Set up logging
logger = config.logger


def read_nrrd(f: Union[str, Iterable],
              threshold: Optional[Union[int, float]] = None,
              include_subdirs: bool = False,
              parallel: Union[bool, int] = 'auto',
              output: Union[Literal['voxels'],
                            Literal['dotprops'],
                            Literal['raw']] = 'voxels',
              errors: Union[Literal['raise'],
                            Literal['log'],
                            Literal['ignore']] = 'log',
              **kwargs) -> 'core.NeuronObject':
    """Create Neuron/List from NRRD file.

    See `here <http://teem.sourceforge.net/nrrd/format.html>`_ for specs of
    NRRD file format including description of the headers.

    Parameters
    ----------
    f :                 str | iterable
                        Filename(s) or folder. If folder, will import all
                        ``.nrrd`` files.
    threshold :         int | float | None
                        For ``output='dotprops'`` only: a threshold to filter
                        low intensity voxels. If ``None``, no threshold is
                        applied and all values > 0 are converted to points.
    include_subdirs :   bool, optional
                        If True and ``f`` is a folder, will also search
                        subdirectories for ``.nrrd`` files.
    parallel :          "auto" | bool | int,
                        Defaults to ``auto`` which means only use parallel
                        processing if more than 10 NRRD files are imported.
                        Spawning and joining processes causes overhead and is
                        considerably slower for imports of small numbers of
                        neurons. Integer will be interpreted as the
                        number of cores (otherwise defaults to
                        ``os.cpu_count() - 2``).
    output :            "voxels" | "dotprops" | "raw"
                        Determines function's output. See Returns.
    errors :            "raise" | "log" | "ignore"
                        If "log" or "ignore", errors will not be raised but
                        instead empty Dotprops will be returned.

    **kwargs
                        Keyword arguments passed to :func:`navis.make_dotprops`
                        if ``output='dotprops'``. Use this to adjust e.g. the
                        number of nearest neighbors used for calculating the
                        tangent vector.

    Returns
    -------
    navis.Voxelneuron
                        If ``output="voxels"`` (default). Contains NRRD header
                        as ``.nrrd_header`` attribute.
    navis.Dotprops
                        If ``output="dotprops"``. Contains NRRD header as
                        ``.nrrd_header`` attribute.
    navis.NeuronList
                        If import of multiple NRRD will return NeuronList of
                        Dotprops/VoxelNeurons.
    (image, header)     (np.ndarray, OrderedDict)
                        If ``output='raw'`` return raw data contained in NRRD
                        file.

    """
    utils.eval_param(output, name='output',
                     allowed_values=('raw', 'dotprops', 'voxels'))

    # If is directory, compile list of filenames
    if isinstance(f, str) and os.path.isdir(f):
        if not include_subdirs:
            f = [os.path.join(f, x) for x in os.listdir(f) if
                 os.path.isfile(os.path.join(f, x)) and x.endswith('.nrrd')]
        else:
            f = [y for x in os.walk(f) for y in glob(os.path.join(x[0], '*.nrrd'))]

    if utils.is_iterable(f):
        # Do not use if there is only a small batch to import
        if isinstance(parallel, str) and parallel.lower() == 'auto':
            if len(f) < 10:
                parallel = False

        if parallel:
            # Do not swap this as ``isinstance(True, int)`` returns ``True``
            if isinstance(parallel, (bool, str)):
                n_cores = os.cpu_count() - 2
            else:
                n_cores = int(parallel)

            with mp.Pool(processes=n_cores) as pool:
                results = pool.imap(_worker_wrapper, [dict(f=x,
                                                           threshold=threshold,
                                                           output=output,
                                                           errors=errors,
                                                           include_subdirs=include_subdirs,
                                                           parallel=False) for x in f],
                                    chunksize=1)

                res = list(config.tqdm(results,
                                       desc='Importing',
                                       total=len(f),
                                       disable=config.pbar_hide,
                                       leave=config.pbar_leave))

        else:
            # If not parallel just import the good 'ole way: sequentially
            res = [read_nrrd(x,
                             threshold=threshold,
                             include_subdirs=include_subdirs,
                             output=output,
                             errors=errors,
                             parallel=parallel,
                             **kwargs)
                   for x in config.tqdm(f, desc='Importing',
                                        disable=config.pbar_hide,
                                        leave=config.pbar_leave)]

        if output == 'raw':
            return [r[0] for r in res], [r[1] for r in res]

        return core.NeuronList([r for r in res if r])

    # Open the file
    fname = os.path.basename(f).split('.')[0]
    data, header = nrrd.read(f)

    if output == 'raw':
        return data, header

    # Try parsing units - this is modelled after the nrrd files you get from
    # Virtual Fly Brain (VFB)
    units = None
    su = None
    voxdim = np.array([1, 1, 1])
    if 'space directions' in header:
        sd = np.asarray(header['space directions'])
        if sd.ndim == 2:
            voxdim = np.diag(sd)[:3]
    if 'space units' in header:
        su = header['space units']
        if len(su) == 3:
            units = [f'{m} {u}' for m, u in zip(voxdim, su)]

    try:
        if output == 'dotprops':
            if threshold:
                data = data >= threshold

            # Data is in voxels - we have to convert it to x/y/z coordinates
            # We need to multiply units first otherwise the KNN will be wrong
            x, y, z = np.where(data)
            points = np.vstack((x, y, z)).T
            points = points * voxdim

            x = core.make_dotprops(points, **kwargs)

            if su and len(su) == 3:
                x.units = [f'1 {s}' for s in su]
        else:
            x = core.VoxelNeuron(data, units=units)
    except BaseException as e:
        msg = f'Error converting file {fname} to neuron.'
        if errors == 'raise':
            raise ImportError(msg) from e
        elif errors == 'log':
            logger.error(f'{msg}: {e}')
        return

    # Add some additional properties
    x.name = fname
    x.origin = f
    x.nrrd_header = header

    return x


def write_nrrd(
    f: Union[str, os.PathLike],
    neuron: 'core.VoxelNeuron',
    attrs: Optional[Dict[str, Any]] = None,
):
    header = getattr(neuron, "nrrd_header", {})

    quant = neuron.units_xyz
    header["space units"] = [str(quant.units)] * len(quant)
    header["space directions"] = np.diag(quant.magnitude).tolist()

    header.update(attrs or {})

    nrrd.write(os.fspath(f), neuron._data, header)


def _worker_wrapper(kwargs):
    """Helper for importing NRRDs using multiple processes."""
    return read_nrrd(**kwargs)
