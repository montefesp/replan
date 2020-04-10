from os.path import join, dirname, abspath
import pickle
from typing import *

import pandas as pd

import pypsa

from shapely.ops import cascaded_union

from src.data.resource.manager import compute_capacity_factors
from src.data.geographics.manager import match_points_to_regions, match_points_to_countries, get_nuts_area, \
    get_onshore_shapes, get_offshore_shapes
from src.data.res_potential.manager import get_capacity_potential_for_regions
from src.data.legacy.manager import get_legacy_capacity_in_regions
from src.resite.resite import Resite
from src.parameters.costs import get_cost

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(asctime)s - %(message)s")
logger = logging.getLogger()


# TODO: does not work for ehighway anymore
def add_generators_from_file(network: pypsa.Network, technologies: List[str], strategy: str, site_nb: int,
                             area_per_site: int, topology_type: str = "countries",
                             cap_dens_dict: Dict[str, float] = None, offshore_buses = True) -> pypsa.Network:
    """Adds wind and pv generator that where selected via a certain siting method to a Network class.

    Parameters
    ----------
    network: pypsa.Network
        A Network instance with regions
    strategy: str
        "comp" or "max, strategy used to select the sites
    site_nb: int
        Number of generation sites to add
    area_per_site: int
        Area per site in km2
    topology_type: str
        Can currently be countries (for one node per country topologies) or ehighway (for topologies based on ehighway)
    cap_dens_dict: Dict[str, float] (default: None)
        Dictionary giving per technology the max capacity per km2
    offshore_buses: bool (default: True)
        Whether the network contains offshore buses

    Returns
    -------
    network: pypsa.Network
        Updated network
    """

    accepted_topologies = ["countries", "ehighway"]
    assert topology_type in accepted_topologies, \
        f"Error: Topology type {topology_type} is not one of {accepted_topologies}"

    resite_data_fn = join(dirname(abspath(__file__)),
                          "../../data/resite/generated/" + strategy + "_site_data_" + str(site_nb) + ".p")
    tech_points_cap_factor_df = pickle.load(open(resite_data_fn, "rb"))

    missing_timestamps = set(network.snapshots) - set(tech_points_cap_factor_df.index)
    assert not missing_timestamps, f"Error: Following timestamps are not part of capacity factors {missing_timestamps}"

    for tech in technologies:

        if tech not in set(tech_points_cap_factor_df.columns.get_level_values(0)):
            print(f"Warning: Technology {tech} is not part of RES data from files. Therefore it was not added.")
            continue

        points = list(tech_points_cap_factor_df[tech].columns)

        # If there are no offshore buses, add all generators to onshore buses
        # If there are offshore buses, add onshore techs to onshore buses and offshore techs to offshore buses
        buses = network.buses.copy()
        if offshore_buses:
            is_onshore = False if tech in ['wind_offshore', 'wind_floating'] else True
            buses = buses[buses.onshore == is_onshore]

        # Compute capacity potential
        if cap_dens_dict is None or tech not in cap_dens_dict:

            assert tech not in ["wind_offshore", "wind_floating"], \
                "Error: Capacities per km for offshore technologies must be pre-specified."

            bus_capacity_potential = get_capacity_potential_for_regions({tech: buses.region.values})[tech]
            bus_capacity_potential.index = buses.index

            # Convert to capacity per km
            if topology_type == "countries":
                nuts_area = get_nuts_area()["2016"]
                uk_el_to_gb_gr = {'UK': 'GB', 'EL': 'GR'}
                nuts_area.index = [uk_el_to_gb_gr[c] if c in uk_el_to_gb_gr else c for c in nuts_area.index]
                bus_capacity_potential_per_km = bus_capacity_potential/nuts_area[bus_capacity_potential.index]
            else:  # topology_type == "ehighway"
                # TODO: need to be implemented
                pass
        else:
            bus_capacity_potential_per_km = pd.Series(cap_dens_dict[tech], index=buses.index)

        # Detect to which bus the node should be associated
        # TODO: this condition is shitty
        if not offshore_buses and topology_type == "countries" and tech in ["wind_offshore", "wind_floating"]:
            points_bus_ds = match_points_to_countries(points, list(buses.index)).dropna()
        else:
            points_bus_ds = match_points_to_regions(points, buses.region).dropna()
        points = list(points_bus_ds.index)

        # Get potential capacity for each point
        points_capacity_potential = \
            [bus_capacity_potential_per_km[points_bus_ds[point]]*area_per_site for point in points]

        # Get capacity factors
        cap_factor_series = tech_points_cap_factor_df.loc[network.snapshots][tech][points].values

        capital_cost, marginal_cost = get_cost(tech, len(network.snapshots))

        network.madd("Generator",
                     "Gen " + tech + " " + pd.Index([str(x) for x, _ in points]) + "-" +
                     pd.Index([str(y) for _, y in points]),
                     bus=points_bus_ds.values,
                     p_nom_extendable=True,
                     p_nom_max=points_capacity_potential,
                     p_max_pu=cap_factor_series,
                     type=tech,
                     x=[x for x, _ in points],
                     y=[y for _, y in points],
                     marginal_cost=marginal_cost,
                     capital_cost=capital_cost)

    return network


def add_generators(network: pypsa.Network, technologies: List[str],
                   params: Dict[str, Any], tech_config: Dict[str, Any], region: str,
                   topology_type: str = 'countries', offshore_buses: bool = True,
                   output_dir: str = None) -> pypsa.Network:
    """
    This function will add generators for different technologies at a series of location selected via an optimization
    mechanism.

    Parameters
    ----------
    network: pypsa.Network
        A network with region associated to each buses.
    region: str
        Region over which the network is defined
    topology_type: str
        Can currently be countries (for one node per country topologies) or ehighway (for topologies based on ehighway)
    offshore_buses: bool (default: True)
        Whether the network contains offshore buses
    output_dir: str
        Absolute path to directory where resite output should be stored

    Returns
    -------
    network: pypsa.Network
        Updated network
    """

    accepted_topologies = ["countries", "ehighway"]
    assert topology_type in accepted_topologies, \
        f"Error: Topology type {topology_type} is not one of {accepted_topologies}"

    logger.info('Setting up resite.')
    resite = Resite([region], technologies, tech_config, params["timeslice"], params["spatial_resolution"],
                    params["keep_files"])

    resite.build_input_data(params['use_ex_cap'], params['filtering_layers'])

    logger.info('resite model being built.')
    resite.build_model(params["modelling"], params['formulation'], params['deployment_vector'], params['write_lp'])

    logger.info('Sending resite to solver.')
    resite.solve_model()

    logger.info('Retrieving resite results.')
    tech_location_dict = resite.retrieve_solution()
    existing_cap_ds, cap_potential_ds, cap_factor_df = resite.retrieve_sites_data()

    logger.info("Saving resite results")
    resite.save(params, output_dir)

    if not resite.timestamps.equals(network.snapshots):
        # If network snapshots is a subset of resite snapshots just crop the data
        # TODO: probably need a better condition
        if network.snapshots[0] in resite.timestamps and network.snapshots[-1] in resite.timestamps:
            cap_factor_df = cap_factor_df.loc[network.snapshots]
        else:
            # In other case, need to recompute capacity factors
            # TODO: to be implemented
            pass

    for tech, points in tech_location_dict.items():

        buses = network.buses.copy()
        if offshore_buses or tech in ["pv_residential", "pv_utility", "wind_onshore"]:
            is_onshore = False if tech in ['wind_offshore', 'wind_floating'] else True
            buses = buses[buses.onshore == is_onshore]
            associated_buses = match_points_to_regions(points, buses.region).dropna()
        elif topology_type == 'countries':
            associated_buses = match_points_to_countries(points, list(buses.index)).dropna()
        else:
            raise ValueError("If you are not using a one-node-per-country topology, you must define offshore buses.")

        points = list(associated_buses.index)

        existing_cap = 0
        if params['use_ex_cap']:
            existing_cap = existing_cap_ds[tech][points].values

        p_nom_max = 'inf'
        if params['limit_max_cap']:
            p_nom_max = cap_potential_ds[tech][points].values

        capital_cost, marginal_cost = get_cost(tech, len(network.snapshots))

        network.madd("Generator",
                     "Gen " + tech + " " + pd.Index([str(x) for x, _ in points]) + "-" +
                     pd.Index([str(y) for _, y in points]),
                     bus=associated_buses.values,
                     p_nom_extendable=True,
                     p_nom_max=p_nom_max,
                     p_nom=existing_cap,
                     p_nom_min=existing_cap,
                     p_max_pu=cap_factor_df[tech][points].values,
                     type=tech,
                     x=[x for x, _ in points],
                     y=[y for _, y in points],
                     marginal_cost=marginal_cost,
                     capital_cost=capital_cost)

    return network


# TODO: add existing capacity,
def add_generators_at_resolution(network: pypsa.Network, technologies: List[str], regions: List[str],
                                 tech_config: Dict[str, Any], spatial_resolution: float,
                                 filtering_layers: Dict[str, bool], use_ex_cap: bool,
                                 topology_type: str = 'countries', offshore_buses: bool = True,) -> pypsa.Network:
    """
    Creates pv and wind generators for every coordinate at a resolution of 0.5 inside the region associate to each bus
    and attach them to the corresponding bus.

    Parameters
    ----------
    network: pypsa.Network
        A PyPSA Network instance with buses associated to regions
    topology_type: str
        Can currently be countries (for one node per country topologies) or ehighway (for topologies based on ehighway)
    offshore_buses: bool (default: True)
        Whether the network contains offshore buses

    Returns
    -------
    network: pypsa.Network
        Updated network
    """

    accepted_topologies = ["countries", "ehighway"]
    assert topology_type in accepted_topologies, \
        f"Error: Topology type {topology_type} is not one of {accepted_topologies}"

    # Generate input data using resite
    resite = Resite(regions, technologies, tech_config, [network.snapshots[0], network.snapshots[-1]],
                    spatial_resolution, False)
    resite.build_input_data(use_ex_cap, filtering_layers)

    for tech in technologies:

        points = resite.tech_points_dict[tech]

        buses = network.buses.copy()
        if offshore_buses or tech in ["pv_residential", "pv_utility", "wind_onshore"]:
            is_onshore = False if tech in ['wind_offshore', 'wind_floating'] else True
            buses = buses[buses.onshore == is_onshore]
            associated_buses = match_points_to_regions(points, buses.region).dropna()
        elif topology_type == 'countries':
            associated_buses = match_points_to_countries(points, list(buses.index)).dropna()
        else:
            raise ValueError("If you are not using a one-node-per-country topology, you must define offshore buses.")
        points = list(associated_buses.index)

        capital_cost, marginal_cost = get_cost(tech, len(network.snapshots))

        network.madd("Generator",
                     "Gen " + tech + " " + pd.Index([str(x) for x, _ in points]) + "-" +
                     pd.Index([str(y) for _, y in points]),
                     bus=associated_buses.values,
                     p_nom_extendable=True,
                     p_nom_max=resite.cap_potential_ds[tech][points].values,
                     p_max_pu=resite.cap_factor_df[tech][points].values,
                     type=tech,
                     x=[x for x, _ in points],
                     y=[y for _, y in points],
                     marginal_cost=marginal_cost,
                     capital_cost=capital_cost)

    return network


def add_generators_per_bus(network: pypsa.Network, technologies: List[str], countries: List[str],
                           tech_config: Dict[str, Any], use_ex_cap: bool = True,
                           offshore_buses: bool = True) -> pypsa.Network:
    """
    Adds pv and wind generators to each bus of a PyPSA Network, each bus being associated to a geographical region.

    Parameters
    ----------
    network: pypsa.Network
        A PyPSA Network instance with buses associated to regions
    technologies: List[str]
        Technologies to each bus
    countries: List[str]
      List of ISO codes of countries over which the network is defined
    tech_config:
        # TODO: comment
    use_ex_cap: bool (default: True)
        Whether to take into account existing capacity
    offshore_buses: bool (default: True)
        Whether the network contains offshore buses


    Returns
    -------
    network: pypsa.Network
        Updated network
    """

    spatial_res = 0.5
    for tech in technologies:

        # If there are no offshore buses, add all generators to onshore buses
        # If there are offshore buses, add onshore techs to onshore buses and offshore techs to offshore buses
        buses = network.buses.copy()
        if offshore_buses:
            is_onshore = False if tech in ['wind_offshore', 'wind_floating'] else True
            buses = buses[buses.onshore == is_onshore]

        # Compute capacity potential
        # TODO: this will only work for countries topology -> need to add it as argument
        if tech in ['wind_offshore', 'wind_floating'] and not offshore_buses:
            # Get offshore regions
            onshore_shapes_union = cascaded_union(get_onshore_shapes(list(buses.index))["geometry"].values)
            offshore_shapes = get_offshore_shapes(list(buses.index), onshore_shapes_union, filterremote=True)
            buses = buses.loc[offshore_shapes.index]
            regions_shapes = offshore_shapes["geometry"].values
        else:
            regions_shapes = buses.region.values
        cap_pot_ds = get_capacity_potential_for_regions({tech: regions_shapes})

        # Compute capacity factors at buses position
        if tech in ['wind_offshore', 'wind_floating'] and not offshore_buses:
            points = [(round(region_shape.centroid.x/spatial_res)*spatial_res,
                       round(region_shape.centroid.y/spatial_res)*spatial_res)
                      for region_shape in regions_shapes]
        else:
            points = [(round(x/spatial_res)*spatial_res,
                       round(y/spatial_res)*spatial_res)
                      for x, y in buses[["x", "y"]].values]
        cap_factor_df = compute_capacity_factors({tech: points}, tech_config, spatial_res, network.snapshots)

        # Compute legacy capacity (not available for wind_floating)
        legacy_capacities = 0
        if use_ex_cap and tech != "wind_floating":
            if tech in ['wind_offshore'] and not offshore_buses:
                legacy_capacities = get_legacy_capacity_in_regions(tech, pd.Series(regions_shapes), countries).values
            else:
                legacy_capacities = get_legacy_capacity_in_regions(tech, buses.region, countries).values

        # Update capacity potentials if legacy capacity is bigger
        cap_potential = cap_pot_ds[tech].values
        for i in range(len(cap_potential)):
            if cap_potential[i] < legacy_capacities[i]:
                cap_potential[i] = legacy_capacities[i]

        # Get costs
        capital_cost, marginal_cost = get_cost(tech, len(network.snapshots))

        # Adding to the network
        network.madd("Generator", f"Gen {tech} " + buses.index,
                     bus=buses.index,
                     p_nom_extendable=True,
                     p_nom=legacy_capacities,
                     p_nom_min=legacy_capacities,
                     p_nom_max=cap_potential,
                     p_max_pu=cap_factor_df[tech].values,
                     type=tech,
                     x=buses.x.values,
                     y=buses.y.values,
                     marginal_cost=marginal_cost,
                     capital_cost=capital_cost)

    return network


# TODO: to be removed
def add_generators_at_bus_test(network: pypsa.Network, params: Dict[str, Any], tech_config: Dict[str, Any], region: str,
                   output_dir=None) \
        -> pypsa.Network:
    """
    This function will add generators for different technologies at a series of location selected via an optimization
    mechanism.

    Parameters
    ----------
    network: pypsa.Network
        A network with region associated to each buses.
    region: str
        Region over which the network is defined
    output_dir: str
        Absolute path to directory where resite output should be stored

    Returns
    -------
    network: pypsa.Network
        Updated network
    """

    logger.info('Setting up resite.')
    resite = Resite([region], params["technologies"], tech_config, params["timeslice"], params["spatial_resolution"],
                    params["keep_files"])

    resite.build_input_data(params['use_ex_cap'], params['filtering_layers'])

    logger.info('resite model being built.')
    resite.build_model(params["modelling"], params['formulation'], params['deployment_vector'], params['write_lp'])

    logger.info('Sending resite to solver.')
    resite.solve_model(params['solver'], params['solver_options'][params['solver']], params['write_log'])

    logger.info('Retrieving resite results.')
    tech_location_dict = resite.retrieve_solution()
    existing_cap_ds, cap_potential_ds, cap_factor_df = resite.retrieve_sites_data()

    logger.info("Saving resite results")
    resite.save(params, output_dir)

    if not resite.timestamps.equals(network.snapshots):
        # If network snapshots is a subset of resite snapshots just crop the data
        # TODO: probably need a better condition
        if network.snapshots[0] in resite.timestamps and network.snapshots[-1] in resite.timestamps:
            cap_factor_df = cap_factor_df.loc[network.snapshots]
        else:
            # In other case, need to recompute capacity factors
            # TODO: to be implemented
            pass

    for tech, points in tech_location_dict.items():

        if tech in ['wind_offshore', 'wind_floating']:
            offshore_buses = network.buses[network.buses.onshore == False]
            associated_buses = match_points_to_regions(points, offshore_buses.region).dropna()
        else:
            onshore_buses = network.buses[network.buses.onshore]
            associated_buses = match_points_to_regions(points, onshore_buses.region).dropna()

        # Get only one point per bus
        buses_points_df = pd.DataFrame(list(associated_buses.index), index=associated_buses.values)
        buses_points_df["Locations"] = buses_points_df[[0, 1]].apply(lambda x: [(x[0], (x[1]))], axis=1)
        buses_points_df = buses_points_df.drop([0, 1], axis=1)
        one_point_per_bus = buses_points_df.groupby(buses_points_df.index).sum().apply(lambda x: x[0][0], axis=1)
        points = one_point_per_bus.values

        existing_cap = 0
        if params['use_ex_cap']:
            existing_cap = existing_cap_ds[tech][points].values

        p_nom_max = 'inf'
        if params['limit_max_cap']:
            p_nom_max = cap_potential_ds[tech][points].values

        capital_cost, marginal_cost = get_cost(tech, len(network.snapshots))

        network.madd("Generator",
                     "Gen " + tech + " " + pd.Index([str(x) for x, _ in points]) + "-" +
                     pd.Index([str(y) for _, y in points]),
                     bus=one_point_per_bus.index,
                     p_nom_extendable=True,
                     p_nom_max=p_nom_max,
                     p_nom=existing_cap,
                     p_nom_min=existing_cap,
                     p_max_pu=cap_factor_df[tech][points].values,
                     type=tech,
                     x=[x for x, _ in points],
                     y=[y for _, y in points],
                     marginal_cost=marginal_cost,
                     capital_cost=capital_cost)

    return network
