from os.path import join, dirname, abspath, isdir
from os import makedirs
from shapely.ops import cascaded_union
from shapely.geometry import MultiPoint
import pandas as pd
from src.data.legacy.manager import get_legacy_capacity
from src.data.resource.manager import read_resource_database, compute_capacity_factors
from src.data.land_data.manager import filter_points
from src.data.res_potential.manager import get_capacity_potential
from src.data.load.manager import retrieve_load_data
from src.data.geographics.manager import return_region_shape, return_points_in_shape, get_subregions
from typing import List, Dict, Tuple
from shutil import copy, rmtree
import yaml
from time import strftime


from src.resite.models.pyomo import build_model as build_pyomo_model, \
    solve_model as solve_pyomo_model, retrieve_sites as retrieve_pyomo_sites
from src.resite.models.docplex import build_model as build_docplex_model, \
    solve_model as solve_docplex_model, retrieve_sites as retrieve_docplex_sites
from src.resite.models.gurobipy import build_model as build_gurobipy_model, \
    solve_model as solve_gurobipy_model, retrieve_sites as retrieve_gurobipy_sites

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s - %(message)s")
logger = logging.getLogger()


class Resite:

    # Pyomo formulation
    build_pyomo_model = build_pyomo_model
    solve_pyomo_model = solve_pyomo_model
    retrieve_pyomo_sites = retrieve_pyomo_sites

    # Docplex formulation
    build_docplex_model = build_docplex_model
    solve_docplex_model = solve_docplex_model
    retrieve_docplex_sites = retrieve_docplex_sites

    # Gurobipy formulation
    build_gurobipy_model = build_gurobipy_model
    solve_gurobipy_model = solve_gurobipy_model
    retrieve_gurobipy_sites = retrieve_gurobipy_sites

    def __init__(self, params):

        if params['formulation'] == 'meet_demand_with_capacity' and len(params['regions']) != 1:
            raise ValueError('The selected formulation works for one region only!')
        elif 'meet_RES_targets' in params['formulation'] and len(params['deployment_vector']) != len(params['regions']):
            raise ValueError('For the selected formulation, the "regions" and "deployment_vector" '
                             'lists must have the same cardinality!')

        self.params = params
        self.logger = logger
        self.keep_files = params['keep_files']
        self.init_output_folder()

        copy(join(dirname(abspath(__file__)), 'config_model.yml'), self.output_folder)
        copy(join(dirname(abspath(__file__)), '../parameters/config_techs.yml'), self.output_folder)

        tech_config_path = join(dirname(abspath(__file__)), '../parameters/config_techs.yml')
        self.tech_config = yaml.load(open(tech_config_path), Loader=yaml.FullLoader)

        self.technologies = params['technologies']
        self.regions = params['regions']
        self.timestamps = pd.date_range(params['timeslice'][0], params['timeslice'][1], freq='1H')
        self.spatial_res = params['spatial_resolution']

        self.instance = None

    def init_output_folder(self):
        """Initialize an output folder."""

        dir_name = "../../output/resite/"
        if not isdir(dir_name):
            makedirs(abspath(dir_name))

        self.output_folder = abspath(dir_name + str(strftime("%Y%m%d_%H%M%S")))
        makedirs(self.output_folder)

        self.logger.info('Folder path is: {}'.format(str(self.output_folder)))

        if not self.keep_files:
            self.logger.info('WARNING! Files will be deleted at the end of the run.')

    def __del__(self):
        """If self.keep_files is false, remove all outputs created during the run."""
        if not self.keep_files:
            rmtree(self.output_folder)

    def build_input_data(self, filtering_layers: Dict[str, bool]):
        """Data pre-processing.

        Parameters:
        -----------
        filtering_layers: Dict[str, bool]
            Dictionary indicating if a given filtering layers needs to be applied. If the layer name is present as key and
            associated to a True boolean, then the corresponding is applied.
        """

        self.logger.info("Loading load")
        self.load_df = retrieve_load_data(self.regions, self.timestamps)

        self.logger.info("Getting region shapes")
        region_shapes = pd.DataFrame(index=self.regions, columns=['full'])
        all_subregions = []
        for region in self.regions:
            subregions = get_subregions(region)
            all_subregions += subregions
            shapes = return_region_shape(region, subregions)
            region_shapes.loc[region, 'full'] = cascaded_union([shapes['onshore'], shapes['offshore']])
        regions_shapes_union = cascaded_union(region_shapes['full'].values)

        # TODO: Need to remove the first init_points by downloading new data
        path_resource_data = join(dirname(abspath(__file__)), '../../data/resource/' + str(self.spatial_res))
        database = read_resource_database(path_resource_data)
        init_points = list(zip(database.longitude.values, database.latitude.values))
        init_points = return_points_in_shape(regions_shapes_union, self.spatial_res, init_points)

        self.logger.info("Filtering coordinates")
        self.tech_points_dict = filter_points(self.technologies, self.tech_config, init_points, self.spatial_res,
                                              filtering_layers)

        self.logger.info("Get existing legacy capacity")
        tech_with_legacy_data = list(set(self.technologies).intersection(['wind_onshore', 'wind_offshore', 'pv_utility']))
        existing_capacity_dict = get_legacy_capacity(tech_with_legacy_data, all_subregions, init_points, self.spatial_res)

        # Update filtered points
        for tech in existing_capacity_dict:
            if existing_capacity_dict[tech] is not None:
                self.tech_points_dict[tech] += list(existing_capacity_dict[tech].keys())
            # Remove duplicates
            self.tech_points_dict[tech] = list(set(self.tech_points_dict[tech]))

        # Remove techs that have no points associated to them
        self.tech_points_dict = {k: v for k, v in self.tech_points_dict.items() if len(v) > 0}

        # Associating coordinates to regions
        # regions_coords_dict = {region: set() for region in regions}
        self.region_tech_points_dict = {region: set() for region in self.regions}
        for tech, coords in self.tech_points_dict.items():
            coords_multipoint = MultiPoint(coords)
            for region in self.regions:
                coords_in_region = coords_multipoint.intersection(region_shapes.loc[region, 'full'])
                coords_in_region = [(tech, (point.x, point.y)) for point in coords_in_region] \
                    if isinstance(coords_in_region, MultiPoint) \
                    else [(tech, (coords_in_region.x, coords_in_region.y))]
                self.region_tech_points_dict[region] = self.region_tech_points_dict[region].union(set(coords_in_region))

        # Create dataframe with existing capacity
        self.tech_points_tuples = [(tech, point) for tech, points in self.tech_points_dict.items() for point in points]
        self.existing_capacity_ds = pd.Series(0., index=pd.MultiIndex.from_tuples(self.tech_points_tuples))
        for tech, coord in self.existing_capacity_ds.index:
            if tech in existing_capacity_dict and existing_capacity_dict[tech] is not None \
                    and coord in existing_capacity_dict[tech]:
                self.existing_capacity_ds[tech, coord] = existing_capacity_dict[tech][coord]

        self.logger.info("Compute cap factor")
        self.cap_factor_df = compute_capacity_factors(self.tech_points_dict, self.tech_config,
                                                      self.spatial_res, self.timestamps)

        self.logger.info("Compute capacity potential per node")
        self.cap_potential_ds = get_capacity_potential(self.tech_points_dict, self.spatial_res, self.regions,
                                                       self.existing_capacity_ds)

        # Compute percentage of existing capacity and set to 1. when capacity is zero
        existing_cap_percentage_ds = self.existing_capacity_ds.divide(self.cap_potential_ds)
        self.existing_cap_percentage_ds = existing_cap_percentage_ds.fillna(1.)

        # Maximum generation that can be produced if max capacity installed
        self.generation_potential_df = self.cap_factor_df * self.cap_potential_ds

    def build_model(self, modelling: str, formulation: str, deployment_vector: List[float], write_lp: bool = False):
        """Model build-up.

        Parameters:
        ------------
        modelling: str
            Choice of modelling language
        formulation: str
            Formulation of the optimization problem to solve
        deployment_vector: List[float]
            # TODO: this is dependent on the formulation so maybe we should create a different function for each formulation
        output_folder: str
            Path towards output folder
        write_lp : bool (default: False)
            If True, the model is written to an .lp file.
        """

        accepted_modelling = ['pyomo', 'docplex', 'gurobipy']
        assert modelling in accepted_modelling, f"Error: {modelling} is not available as modelling language. " \
                                                f"Accepted languages are {accepted_modelling}"
        self.modelling = modelling
        if self.modelling == 'pyomo':
            self.build_pyomo_model(formulation, deployment_vector, write_lp)
        elif self.modelling == 'docplex':
            self.build_docplex_model(formulation, deployment_vector, write_lp)
        elif self.modelling == 'gurobipy':
            self.build_gurobipy_model(formulation, deployment_vector, write_lp)

    def solve_model(self, solver, solver_options):
        """
        Solve a model

        Parameters
        ----------
        solver: str
            Name of the solver to use
        solver_options: Dict[str, float]
            Dictionary of solver options name and value

        """
        if self.modelling == 'pyomo':
            self.solve_pyomo_model(solver, solver_options)
        elif self.modelling == 'docplex':
            self.solve_docplex_model(solver, solver_options)
        elif self.modelling == 'gurobipy':
            self.solve_gurobipy_model(solver, solver_options)

    def retrieve_sites(self, save_file: bool) -> Dict[str, List[Tuple[float, float]]]:
        """
        Get points that were selected during the optimization

        Parameters
        ----------
        save_file: bool
            Whether to save the results in the output folder or not

        Returns
        -------
        Dict[str, List[Tuple[float, float]]]
            Lists of selected points for each technology

        """
        if self.modelling == 'pyomo':
            self.selected_tech_points_dict = self.retrieve_pyomo_sites(save_file)
        elif self.modelling == 'docplex':
            self.selected_tech_points_dict = self.retrieve_docplex_sites(save_file)
        elif self.modelling == 'gurobipy':
            self.selected_tech_points_dict = self.retrieve_gurobipy_sites(save_file)

        return self.selected_tech_points_dict

    def retrieve_sites_data(self):
        """
        This function returns the data for the optimal sites.

        Returns
        -------
        self.selected_existing_capacity_ds: pd.Series
            Pandas series giving for each (tech, coord) tuple in self.selected_tech_points_dict the existing
            capacity at these positions
        self.selected_capacity_potential_ds: pd.Series
            Pandas series giving for each (tech, coord) tuple in self.selected_tech_points_dict the capacity
            potential at these positions .
        self.selected_cap_factor_df: pd.DataFrame
            Pandas series indexed by time giving for each (tech, coord) tuple in self.selected_tech_points_dict
            its capacity factors time series

        """

        selected_tech_points_tuples = [(tech, point) for tech, points in self.selected_tech_points_dict.items()
                                       for point in points]

        self.selected_existing_capacity_ds = self.existing_capacity_ds.loc[selected_tech_points_tuples]
        self.selected_capacity_potential_ds = self.cap_potential_ds.loc[selected_tech_points_tuples]
        self.selected_cap_factor_df = self.cap_factor_df[selected_tech_points_tuples]

        return self.selected_existing_capacity_ds, self.selected_capacity_potential_ds, self.selected_cap_factor_df

    def save(self):
        pass



