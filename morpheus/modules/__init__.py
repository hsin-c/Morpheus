# Copyright (c) 2022-2023, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Morpheus module definitions, each module is automatically registered when imported
"""
# When segment modules are imported, they're added to the module registry.
# To avoid flake8 warnings about unused code, the noqa flag is used during import.
from morpheus.modules import file_batcher
from morpheus.modules import file_to_df
from morpheus.modules import filter_control_message
from morpheus.modules import filter_detections
from morpheus.modules import from_control_message
from morpheus.modules import mlflow_model_writer
from morpheus.modules import serialize
from morpheus.modules import to_control_message
from morpheus.modules import write_to_file
from morpheus._lib import modules

__all__ = []
