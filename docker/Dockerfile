# syntax=docker/dockerfile:1.3

# SPDX-FileCopyrightText: Copyright (c) 2021-2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# ============== LEGEND =================================================== #
#                         [conda_bld_morpheus]
#                               /
# [base] -> [conda_env] -> [base_extended] -> [runtime]
#                               \
#                         [conda_env_dev] -> [development] -> [development_pydbg]
#
#  base: Contains all system packages common across all environments
#  conda_env: Create morpheus conda environment and set it as the new base
#  base_extended: Optional environmental elements, libraries, packages, etc.
#  conda_env_dev: Full development environment with all necessary conda
#    packages.
#  conda_bld_morpheus: Utility stage used to build and cache the Morpheus
#    conda package for runtime environments. (should not be extended)
#  runtime: Full runtime environment with Morpheus installed as a conda
#    package
#  development: Full development environment able to build against a Morpheus
#    source directory.
#  development_pydbg: Development + debug build of cpython and various GDB
#    debugging macros.
# ========================================================================= #

# Args used in FROM commands must come first
ARG FROM_IMAGE="nvidia/cuda"
ARG CUDA_MAJOR_VER=11
ARG CUDA_MINOR_VER=8
ARG CUDA_REV_VER=0
ARG LINUX_DISTRO=ubuntu
ARG LINUX_VER=22.04
ARG PYTHON_VER=3.10

# ============ Stage: base ============
# Configure the base conda environment
FROM ${FROM_IMAGE}:${CUDA_MAJOR_VER}.${CUDA_MINOR_VER}.${CUDA_REV_VER}-base-${LINUX_DISTRO}${LINUX_VER} AS base

# Required arguments
ARG PYTHON_VER
ARG IMAGE_TYPE=base
ARG RAPIDS_CHANNEL=rapidsai-nightly
ARG RAPIDS_VER=23.02
ARG CONDA_CHANNEL=rapidsai
ARG CUDA_MAJOR_VER
ARG CUDA_MINOR_VER

# Temp option to set conda channel_alias property during build
ARG CONDA_CHANNEL_ALIAS

# The TensorRT Version must be the full version (i.e. 7.2.2.3) and match Triton EXACTLY
ARG TENSORRT_VERSION=8.2.1.3

ENV DEBIAN_FRONTEND=noninteractive

# Remove the file which breaks apt caching since we will handle it ourselves. See
# https://vsupalov.com/buildkit-cache-mount-dockerfile/
RUN rm -f /etc/apt/apt.conf.d/docker-clean

# Install dependencies to build
RUN --mount=type=cache,id=apt,target=/var/cache/apt \
    apt-get update &&\
    apt-get upgrade -y &&\
    apt-get install --no-install-recommends -y \
        bc \
        build-essential \
        cuda-compiler-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cudart-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cupti-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-gdb-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        curl \
        git-lfs \
        jq \
        libcublas-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcufft-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcurand-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcusolver-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libnuma1 \
        openssh-client \
        pkg-config \
        tar \
        unzip \
        wget \
        zip \
    && rm -rf /var/lib/apt/lists/*

# Enables "source activate conda"
SHELL ["/bin/bash", "-c"]

ENV CUDA_PATH=/usr/local/cuda

# All code will be under /workspace
ENV MORPHEUS_ROOT=/workspace
WORKDIR ${MORPHEUS_ROOT}

COPY --from=condaforge/mambaforge:latest /opt/conda /opt/conda

ENV PATH="${PATH}:/opt/conda/bin"

# Install mamba to speed the solve up
RUN --mount=type=cache,id=conda_pkgs,target=/opt/conda/pkgs,sharing=locked \
    conda config --set ssl_verify false &&\
    conda config --add pkgs_dirs /opt/conda/pkgs &&\
    # Install mamba, boa and git here. Conda build breaks with other git installs
    /opt/conda/bin/mamba install -y -n base -c conda-forge "boa" "git >=2.35.3" "git-lfs" "python=${PYTHON_VER}" "tini=0.19" &&\
    source activate base &&\
    git lfs install
    # conda clean -afy

# ============ Stage: conda_env ============
# Create the conda environment and install all dependencies
FROM base as conda_env

# Create a base environment
RUN --mount=type=cache,id=conda_pkgs,target=/opt/conda/pkgs,sharing=locked \
    # Create the environment and install as little dependencies as possible
    CONDA_ALWAYS_YES=true /opt/conda/bin/mamba create -n morpheus -c conda-forge  \
      python=${PYTHON_VER} && \
    # Clean and activate
    # conda clean -afy && \
    conda init bash && \
    echo "conda activate morpheus" >> ~/.bashrc

# Set the permenant conda channes to use for morpheus
RUN source activate morpheus &&\
    conda config --env --add channels conda-forge &&\
    conda config --env --add channels nvidia &&\
    conda config --env --add channels rapidsai

# Set the entrypoint to use the entrypoint.sh script which sets the conda env
COPY docker/entrypoint.sh ./docker/
ENTRYPOINT [ "/opt/conda/bin/tini", "--", "/workspace/docker/entrypoint.sh" ]

# Reset the shell back to normal
SHELL ["/bin/bash", "-c"]

# ============ Stage: base_extended ============
# Base environment with any optional extensions. ex. DOCA libs
FROM conda_env as base_extended

# Add one or more optional dependencies to the base environment
ARG MORPHEUS_SUPPORT_DOCA="FALSE"
ARG DOCA_ARTIFACTS_HOST
ARG DOCA_REPO_HOST
ARG DOCA_BUILD_ID=7930666
ARG DOCA_VERSION=2.2.0002-1
ARG DPDK_VERSION=22.11.0-1.4.1

# Set this environment variable so it auto builds DOCA
ENV MORPHEUS_SUPPORT_DOCA=${MORPHEUS_SUPPORT_DOCA}

# Copy all of the optional dependency scripts
COPY ./docker/optional_deps docker/optional_deps

# Install DOCA (If requested)
RUN --mount=type=cache,id=doca,target=/tmp/doca,sharing=locked \
    --mount=type=cache,id=apt,target=/var/cache/apt \
    ./docker/optional_deps/doca.sh /tmp/doca

# ============ Stage: conda_env_dev ============
# Create the development conda environment
FROM base_extended as conda_env_dev

ARG CONDA_CHANNEL_ALIAS

# Copy the development dependencies file
COPY docker/conda/environments/cuda${CUDA_MAJOR_VER}.${CUDA_MINOR_VER}_dev.yml ./docker/conda/environments/
COPY docker/conda/environments/requirements.txt ./docker/conda/environments/

# Update the morpheus environment
RUN --mount=type=cache,id=conda_pkgs,target=/opt/conda/pkgs,sharing=locked \
    # Temp add channel_alias to get around conda 404 errors
    conda config --env --set channel_alias ${CONDA_CHANNEL_ALIAS:-"https://conda.anaconda.org"} &&\
    /opt/conda/bin/mamba env update -n morpheus --file docker/conda/environments/cuda${CUDA_MAJOR_VER}.${CUDA_MINOR_VER}_dev.yml &&\
    # Remove channel_alias to use the normal channel in the container
    conda config --env --remove-key channel_alias &&\
    # Clean and activate
    conda clean -afy

# ============ Stage: conda_bld_morpheus ============
# Now build the morpheus conda package
FROM base_extended as conda_bld_morpheus

ARG CONDA_CHANNEL_ALIAS
ARG CUDA_MAJOR_VER
ARG CUDA_MINOR_VER

RUN --mount=type=cache,id=apt,target=/var/cache/apt \
    apt update && \
    DEBIAN_FRONTEND=noninteractive TZ=Etc/UTC \
    apt install --no-install-recommends -y \
        cuda-compiler-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cudart-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cupti-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-nvml-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-nvtx-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcublas-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcufft-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcurand-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcusolver-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} && \
    rm -rf /var/lib/apt/lists/*

# Copy the source
COPY . ./

RUN --mount=type=cache,id=workspace_cache,target=/workspace/.cache,sharing=locked \
    --mount=type=cache,id=conda_pkgs,target=/opt/conda/pkgs,sharing=locked \
    source activate base &&\
    # Need to get around recent versions of git locking paths until they are deemed safe
    git config --global --add safe.directory "*" &&\
    MORPHEUS_PYTHON_BUILD_STUBS=OFF CONDA_BLD_PATH=/opt/conda/conda-bld ./ci/conda/recipes/run_conda_build.sh morpheus

# sid_visualization is a submodule we need to init
RUN git submodule update --init --recursive

# ============ Stage: runtime ============
# Setup container for runtime environment
FROM base_extended as runtime

# Manually need to install some pip-only dependencies. Once these can get moved to conda, they can be removed.
COPY docker/conda/environments/requirements.txt ./docker/conda/environments/
COPY docker/conda/environments/cuda${CUDA_MAJOR_VER}.${CUDA_MINOR_VER}_runtime.yml ./docker/conda/environments/

# Mount Morpheus conda package build in `conda_bld_morpheus`
RUN --mount=type=bind,from=conda_bld_morpheus,source=/opt/conda/conda-bld,target=/opt/conda/conda-bld \
    --mount=type=cache,id=conda_pkgs,target=/opt/conda/pkgs,sharing=locked \
    source activate morpheus &&\
    # Install morpheus
    CONDA_ALWAYS_YES=true /opt/conda/bin/mamba install -n morpheus \
        -c local \
        -c rapidsai \
        -c nvidia/label/cuda-11.8.0 \
        -c nvidia/label/dev \
        -c nvidia \
        -c conda-forge morpheus &&\
    # Install runtime dependencies that are pip-only
    /opt/conda/bin/mamba env update -n morpheus --file docker/conda/environments/cuda${CUDA_MAJOR_VER}.${CUDA_MINOR_VER}_runtime.yml

# Only copy specific files/folders over that are necessary for runtime
COPY "./docker" "./docker"
COPY "./docs" "./docs"
COPY "./examples" "./examples"
COPY "./models" "./models"
COPY "./scripts" "./scripts"
COPY ["*.md", "LICENSE", "./"]

# remedy for CVE-2015-20107
RUN find / -name '*mailcap*.*py*' -delete

# Use morpheus by default
CMD [ "morpheus" ]

# ============ Stage: development ============
# Install and configure development only packages
FROM conda_env_dev as development
ARG CUDA_MAJOR_VER
ARG CUDA_MINOR_VER

RUN --mount=type=cache,id=apt,target=/var/cache/apt \
    apt update && \
    TZ=Etc/UTC \
    apt install --no-install-recommends -y \
        cuda-compiler-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cudart-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-cupti-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        cuda-nvtx-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcublas-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcufft-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcurand-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} \
        libcusolver-dev-${CUDA_MAJOR_VER}-${CUDA_MINOR_VER} && \
    rm -rf /var/lib/apt/lists/*

# Install camouflage needed for unittests to mock a triton server
# Pin to v0.9 until #967 is resolved
RUN source activate morpheus && \
    npm install -g camouflage-server@0.9 && \
    npm cache clean --force

# Setup git to allow other users to access /workspace. Requires git 2.35.3 or
# greater. See https://marc.info/?l=git&m=164989570902912&w=2. Only enable for
# development
RUN git config --global --add safe.directory "*"

# ============ Stage: python_debug_bld ===========
# Configure and build cpython with debugging symbols
FROM development as development_pydbg

COPY ci/conda/recipes/python-dbg/ ./ci/conda/recipes/python-dbg
COPY ci/conda/recipes/run_conda_build.sh ./ci/conda/recipes/run_conda_build.sh
COPY ci/conda/recipes/python_dbg_install.sh ./ci/conda/recipes/python_dbg_install.sh

# Temporary until #68 goes in
ARG MORPHEUS_USER="root"

# Build and install debug cpython
RUN source activate morpheus \
    && MORPHEUS_ROOT=/workspace \
       CONDA_BLD_PATH=/opt/conda/conda-bld \
       ./ci/conda/recipes/run_conda_build.sh pydebug \
    && ./ci/conda/recipes/python_dbg_install.sh \
       -s $( ls /opt/conda/conda-bld/src_cache/Python-${PYTHON_VER}*.tar.xz ) \
       -c $( ls /opt/conda/conda-bld/linux-64/python-${PYTHON_VER}*.tar.bz2 ) \
       -i ./ci/conda/recipes/python-dbg/source \
       -g $(eval echo "~${MORPHEUS_USER}")/.gdbinit
