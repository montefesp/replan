from os.path import isdir
from os import makedirs
from time import strftime

from pyomo.opt import ProblemFormat
import argparse

from iepy.topologies.tyndp2018 import get_topology
from iepy.geographics import get_subregions
from iepy.technologies import get_config_dict
from network import *
from postprocessing.results_display import *
from projects.elia.utils import upgrade_topology
from network.globals.functionalities_nopyomo \
    import add_extra_functionalities as add_extra_functionalities_nopyomo

from iepy import data_path

from copy import copy

import logging
logging.basicConfig(level=logging.DEBUG, format=f"%(levelname)s %(name) %(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

NHoursPerYear = 8760.


def parse_args():

    parser = argparse.ArgumentParser(description='Command line arguments.')

    parser.add_argument('-lpd', '--line_price_div', type=float, help='Value by which DC price will be divided')
    parser.add_argument('-owp', '--onshore_wind_price', type=float,
                        help='Value by which onshore wind in Europe will be multiplied')

    def to_bool(string):
        return string == "true"
    parser.add_argument('-elc', '--extend_line_cap', type=to_bool,
                        help='Whether lines can be extended or not', default='true')
    parser.add_argument('-lem', '--line_extension_multiplier', type=float, help='Value indicating how to limit '
                        'transmission investment in Europe')
    parser.add_argument('-th', '--threads', type=int, help='Number of threads', default=1)

    def to_dict(string):
        countries, techs = string.split(":")
        countries = countries.strip("[]").split(",")
        techs = techs.strip("[]").split(",")
        dict = {}
        for country in countries:
            dict[country] = copy(techs)
        return dict
    parser.add_argument('-neu', '--non_eu', type=to_dict, help='Which technology to add outside Europe')

    parsed_args = vars(parser.parse_args())

    return parsed_args


if __name__ == '__main__':

    args = parse_args()
    logger.info(args)

    # Main directories
    data_dir = f"{data_path}"
    tech_dir = f"{data_path}technologies/"
    output_dir = f"output/{strftime('%Y%m%d_%H%M%S')}/"

    # Run config
    config_fn = join(dirname(abspath(__file__)), 'config.yaml')
    config = yaml.load(open(config_fn, 'r'), Loader=yaml.FullLoader)
    # Add args to config
    config = {**config, **args}

    solver_options = config["solver_options"][config["solver"]]
    if config["solver"] == 'gurobi':
        config["solver_options"][config["solver"]]['Threads'] = config['threads']
    else:
        config["solver_options"][config["solver"]]['threads'] = config['threads']

    # Parameters
    tech_info = pd.read_excel(join(tech_dir, 'tech_info.xlsx'), sheet_name='values', index_col=0)
    fuel_info = pd.read_excel(join(tech_dir, 'fuel_info.xlsx'), sheet_name='values', index_col=0)

    # Compute and save results
    if not isdir(output_dir):
        makedirs(output_dir)

    # Save config and parameters files
    yaml.dump(config, open(f"{output_dir}config.yaml", 'w'), sort_keys=False)
    yaml.dump(get_config_dict(), open(f"{output_dir}tech_config.yaml", 'w'), sort_keys=False)
    tech_info.to_csv(f"{output_dir}tech_info.csv")
    fuel_info.to_csv(f"{output_dir}fuel_info.csv")

    # Time
    timeslice = config['time']['slice']
    time_resolution = config['time']['resolution']
    timestamps = pd.date_range(timeslice[0], timeslice[1], freq=f"{time_resolution}H")

    # Building network
    # Add location to Generators and StorageUnits
    override_comp_attrs = pypsa.descriptors.Dict({k: v.copy() for k, v in pypsa.components.component_attrs.items()})
    override_comp_attrs["Generator"].loc["x"] = ["float", np.nan, np.nan, "x in position (x;y)", "Input (optional)"]
    override_comp_attrs["Generator"].loc["y"] = ["float", np.nan, np.nan, "y in position (x;y)", "Input (optional)"]
    override_comp_attrs["StorageUnit"].loc["x"] = ["float", np.nan, np.nan, "x in position (x;y)", "Input (optional)"]
    override_comp_attrs["StorageUnit"].loc["y"] = ["float", np.nan, np.nan, "y in position (x;y)", "Input (optional)"]

    net = pypsa.Network(name="ELIA network", override_component_attrs=override_comp_attrs)
    net.set_snapshots(timestamps)

    # Adding carriers
    for fuel in fuel_info.index[1:-1]:
        net.add("Carrier", fuel, co2_emissions=fuel_info.loc[fuel, "CO2"])

    # Loading topology
    logger.info("Loading topology.")
    countries = get_subregions(config["region"])
    if config["add_TR"]:
        countries = countries + ["TR"]

    net = get_topology(net, countries, extend_line_cap=config["extend_line_cap"],
                       extension_multiplier=config["line_extension_multiplier"], plot=True)

    # Divide DC transmission price
    if config["line_price_div"] is not None:
        dc_links = net.links.carrier == "DC"
        net.links.loc[dc_links, "capital_cost"] /= args["line_price_div"]

    # Adding load
    logger.info("Adding load.")
    # onshore_bus_indexes = net.buses[net.buses.onshore].index
    load = get_load(timestamps=timestamps, countries=countries, missing_data='interpolate')
    load_indexes = "Load " + pd.Index(countries)
    loads = pd.DataFrame(load.values, index=net.snapshots, columns=load_indexes)
    net.madd("Load", load_indexes, bus=countries, p_set=loads)

    if config["functionalities"]["load_shed"]["include"]:
        logger.info("Adding load shedding generators.")
        net = add_load_shedding(net, loads)

    # Adding pv and wind generators
    if config['res']['include']:
        for strategy, technologies in config['res']['strategies'].items():
            # If no technology is associated to this strategy, continue
            if not len(technologies):
                continue

            logger.info(f"Adding RES {technologies} generation with strategy {strategy}.")

            if strategy == "bus":
                net = add_res_per_bus(net, 'countries', technologies, config["res"]["use_ex_cap"])
            elif strategy == "no_siting":
                net = add_res_in_grid_cells(net, 'countries', technologies,
                                            config["region"], config["res"]["spatial_resolution"],
                                            config["res"]["use_ex_cap"], config["res"]["limit_max_cap"])
            elif strategy == 'siting':
                net = add_res(net, 'countries', technologies, config["region"], config['res'],
                              config['res']['use_ex_cap'], config['res']['limit_max_cap'],
                              output_dir=f"{output_dir}resite/")

    # Increase wind onshore price
    if config["onshore_wind_price"] is not None:
        onshore_wind_gens = net.generators.type.str.startswith("wind_onshore")
        net.generators.loc[onshore_wind_gens, "capital_cost"] *= config["onshore_wind_price"]

    # Add conventional gen
    if "dispatch" in config["techs"]:
        net = add_conventional(net, config["techs"]["dispatch"]["tech"])

    # Adding nuclear
    if "nuclear" in config["techs"]:
        net = add_nuclear(net, countries,
                          config["techs"]["nuclear"]["use_ex_cap"],
                          config["techs"]["nuclear"]["extendable"])

    if "sto" in config["techs"]:
        net = add_sto_plants(net, 'countries',
                             config["techs"]["sto"]["extendable"],
                             config["techs"]["sto"]["cyclic_sof"])

    if "phs" in config["techs"]:
        net = add_phs_plants(net, 'countries',
                             config["techs"]["phs"]["extendable"],
                             config["techs"]["phs"]["cyclic_sof"])

    if "ror" in config["techs"]:
        net = add_ror_plants(net, 'countries', config["techs"]["ror"]["extendable"])

    if "battery" in config["techs"]:
        net = add_batteries(net, config["techs"]["battery"]["type"], countries)

    # Adding non-European nodes with generation capacity
    non_eu_res = config["non_eu"] #["res"]
    if non_eu_res is not None:
        net = upgrade_topology(net, list(non_eu_res.keys()))
        # Add generation and storage
        for region in non_eu_res.keys():
            if region in ["NA", "ME"]:
                countries = get_subregions(region)
            else:
                countries = [region]
            topology_type = 'regions' if region == "GL" else 'countries'
            res_techs = non_eu_res[region]
            net = add_res_per_bus(net, topology_type, res_techs, bus_ids=countries)
            net = add_batteries(net, config["battery"]["type"], countries)

    if config["get_duals"]:
        net.lopf(solver_name=config["solver"],
                 solver_logfile=f"{output_dir}solver.log",
                 solver_options=config["solver_options"][config["solver"]],
                 extra_functionality=add_extra_functionalities_nopyomo,
                 keep_references=True,
                 keep_shadowprices=["Generator", "Bus"], pyomo=False)
    else:
        net.lopf(solver_name=config["solver"],
                 solver_logfile=f"{output_dir}solver.log",
                 solver_options=config["solver_options"][config["solver"]],
                 extra_functionality=add_extra_functionalities,
                 pyomo=True)

    if config['keep_lp']:
        net.model.write(filename=join(output_dir, 'model.lp'),
                        format=ProblemFormat.cpxlp,
                        # io_options={'symbolic_solver_labels': True})
                        io_options={'symbolic_solver_labels': False})
        net.model.objective.pprint()

    net.export_to_csv_folder(output_dir)