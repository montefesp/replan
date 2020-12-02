import pandas as pd

import pypsa
from pypsa.linopt import get_var, linexpr, define_constraints

from iepy.technologies import get_fuel_info, get_tech_info, get_config_values
from iepy.indicators.emissions import get_reference_emission_levels_for_region


def add_co2_budget_global(net: pypsa.Network, region: str, co2_reduction_share: float, co2_reduction_refyear: int):
    """
    Add global CO2 budget.

    Parameters
    ----------
    region: str
        Region over which the network is defined.
    net: pypsa.Network
        A PyPSA Network instance with buses associated to regions
    co2_reduction_share: float
        Percentage of reduction of emission.
    co2_reduction_refyear: int
        Reference year from which the reduction in emission is computed.

    """

    # TODO: this is coded like shit
    # Get different techs co2 emissions
    co2_techs = ['ccgt']
    co2_techs_emissions = dict.fromkeys(co2_techs)
    for tech in co2_techs:
        fuel, efficiency = get_tech_info(tech, ["fuel", "efficiency_ds"])
        fuel_emissions_el = get_fuel_info(fuel, ['CO2'])
        # TODO: why are we doing this? isn't this wrong?
        fuel_emissions_thermal = fuel_emissions_el / efficiency
        co2_techs_emissions[tech] = fuel_emissions_thermal.values[0]

    co2_reference_kt = get_reference_emission_levels_for_region(region, co2_reduction_refyear)
    co2_budget = co2_reference_kt * (1 - co2_reduction_share) * len(net.snapshots) / 8760.

    gens = net.generators[(net.generators.type.str.contains('|'.join(co2_techs)))]

    gens_p = get_var(net, 'Generator', 'p')[gens.index]

    coeff = pd.DataFrame(index=gens_p.index, columns=gens_p.columns, dtype=float)
    for tech in co2_techs:
        gens_with_tech = gens[gens.index.str.contains(tech)]
        coeff[gens_with_tech.index] = co2_techs_emissions[tech]

    lhs = linexpr((coeff, gens_p)).sum().sum()

    define_constraints(net, lhs, '<=', co2_budget, 'generation_emissions_global')


def add_import_limit_constraint(net: pypsa.Network, import_share: float):
    """
    Add per-bus constraint on import budgets.

    Parameters
    ----------
    net: pypsa.Network
        A PyPSA Network instance with buses associated to regions
    import_share: float
        Maximum share of load that can be satisfied via imports.

    Notes
    -----
    Using a flat value across EU, could be updated to support different values for different countries
    """

    # Get links flow variables
    links_p = get_var(net, 'Link', 'p')

    # For each bus, add an import constraint
    for bus in net.loads.bus:
        # Compute net imports
        links_in = net.links[net.links.bus1 == bus].index
        links_out = net.links[net.links.bus0 == bus].index
        links_connected = list(links_in) + list(links_out)
        # Coefficient allow to differentiate between imports and exports
        coefficients = pd.Series(1, index=links_connected, dtype=int)
        for link in links_connected:
            if link in links_out:
                coefficients.loc[link] *= -1
        net_imports = linexpr((coefficients, links_p[links_connected])).sum().sum()

        # Get load for country
        load_idx = net.loads[net.loads.bus == bus].index
        load = net.loads_t.p_set[load_idx].sum()

        define_constraints(net, net_imports, '<=', load*import_share, 'import_limit', bus)


def store_links_constraint(net: pypsa.Network, ctd_ratio: float):

    links_p_nom = get_var(net, 'Link', 'p_nom')

    links_to_bus = links_p_nom[links_p_nom.index.str.contains('to AC')].index
    links_from_bus = links_p_nom[links_p_nom.index.str.contains('AC to')].index

    for pair in list(zip(links_to_bus, links_from_bus)):

        discharge_link = links_p_nom.loc[pair[0]]
        charge_link = links_p_nom.loc[pair[1]]
        lhs = linexpr((ctd_ratio, discharge_link), (-1., charge_link))

        define_constraints(net, lhs, '==', 0., 'store_links_constraint')


def add_extra_functionalities(net: pypsa.Network, snapshots: pd.DatetimeIndex):
    """
    Wrapper for the inclusion of multiple extra_functionalities.

    Parameters
    ----------
    net: pypsa.Network
        A PyPSA Network instance with buses associated to regions
        and containing a functionality configuration dictionary
    snapshots: pd.DatetimeIndex
        Network snapshots.

    """

    conf_func = net.config["functionalities"]

    if conf_func["co2_emissions"]["include"]:
        strategy = conf_func["co2_emissions"]["strategy"]
        mitigation_factor = conf_func["co2_emissions"]["mitigation_factor"]
        ref_year = conf_func["co2_emissions"]["reference_year"]
        if strategy == 'country':
            # TODO: to be implemented
            add_co2_budget_per_country(net, mitigation_factor, ref_year)
        elif strategy == 'global':
            add_co2_budget_global(net, net.config["region"], mitigation_factor, ref_year)

    if conf_func["import_limit"]["include"]:
        add_import_limit_constraint(net, conf_func["import_limit"]["share"])

    if not net.config["techs"]["battery"]["fixed_duration"]:
        ctd_ratio = get_config_values("Li-ion_p", ["ctd_ratio"])
        store_links_constraint(net, ctd_ratio)
